package com.verabank.transfer.transfer;

import com.verabank.transfer.account.AccountRepository;
import com.verabank.transfer.account.AccountRow;
import com.verabank.transfer.audit.AuditWriter;
import com.verabank.transfer.config.VerabankProperties;
import com.verabank.transfer.scoring.FraudScorer;
import com.verabank.transfer.scoring.ScoreRequest;
import com.verabank.transfer.scoring.ScoreResult;
import com.verabank.transfer.web.ApiException;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataAccessException;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.TransactionException;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * Creación de transferencias según §2.3 y ADR-03.
 *
 * <pre>
 * tx1  insertar PENDING (unicidad client_id + clave) · retener fondos · saga FUNDS_HELD
 *      · outbox(TransferInitiated) · auditoría
 * --   score = FraudScorer.score(req)            (fuera de transacción)
 * tx2  APPROVE + INTERNAL: asientos D/C, liberar la retención consumida · COMPLETED
 *      · outbox(TransferCompleted) · auditoría
 * </pre>
 *
 * Si tx2 falla, se revierte entera: la transferencia sigue PENDING con su retención (no hay
 * retención huérfana) y la saga queda con wake_at vencido tras {@code verabank.saga.resume-after};
 * un reintento con la misma clave pasado ese plazo la reanuda (score idempotente por
 * transfer_id). El poller de la fase 2 hará lo mismo sin esperar al cliente.
 */
@Service
public class TransferService {

    private static final Logger log = LoggerFactory.getLogger(TransferService.class);
    static final String SYSTEM_ACTOR = "system:transfer-service";

    private final TransferRepository transfers;
    private final AccountRepository accounts;
    private final LedgerRepository ledger;
    private final SagaRepository sagas;
    private final TransferEvents events;
    private final AuditWriter audit;
    private final FraudScorer scorer;
    private final TransactionTemplate tx;
    private final Duration resumeAfter;

    public TransferService(TransferRepository transfers, AccountRepository accounts, LedgerRepository ledger,
            SagaRepository sagas, TransferEvents events, AuditWriter audit, FraudScorer scorer,
            PlatformTransactionManager txManager, VerabankProperties properties) {
        this.transfers = transfers;
        this.accounts = accounts;
        this.ledger = ledger;
        this.sagas = sagas;
        this.events = events;
        this.audit = audit;
        this.scorer = scorer;
        this.tx = new TransactionTemplate(txManager);
        this.tx.setIsolationLevel(TransactionDefinition.ISOLATION_READ_COMMITTED);
        this.resumeAfter = properties.saga().resumeAfter();
    }

    /** Resultado para el controlador: la vista y si la creó esta solicitud (201) o es un reintento (200). */
    public record Outcome(TransferView transfer, boolean replayed) {
    }

    public Outcome create(String clientId, UUID idempotencyKey, NewTransfer request, UUID correlationId) {
        String hash = RequestHasher.hash(request);
        UUID transferId = UUID.randomUUID();

        Tx1 first = tx.execute(status -> openTransfer(transferId, clientId, idempotencyKey, hash, request,
                correlationId));
        if (first.existing != null) {
            return replay(first.existing, hash, correlationId);
        }
        TransferRow opened = first.created;
        if (opened.status() == TransferStatus.REJECTED) {
            throw rejection(RejectionReason.valueOf(opened.failureReason()));
        }
        TransferRow closed = close(opened, correlationId);
        if (closed.status() == TransferStatus.REJECTED) {
            throw rejection(RejectionReason.valueOf(closed.failureReason()));
        }
        return new Outcome(TransferView.of(closed), false);
    }

    public Optional<TransferView> find(UUID transferId) {
        return transfers.find(transferId).map(TransferView::of);
    }

    // ---------------------------------------------------------------- tx1

    private record Tx1(TransferRow created, TransferRow existing) {
    }

    private Tx1 openTransfer(UUID transferId, String clientId, UUID key, String hash, NewTransfer r,
            UUID correlationId) {
        Optional<TransferRow> existing = transfers.findByKey(clientId, key);
        if (existing.isPresent()) {
            return new Tx1(null, existing.get());
        }
        AccountRow source = customerAccount(r.sourceAccountId());
        AccountRow dest = r.rail() == Rail.INTERNAL ? customerAccount(r.destAccountId()) : null;
        if (!source.currency().equals(r.currency()) || (dest != null && !dest.currency().equals(r.currency()))) {
            throw ApiException.validation("currency no coincide con la moneda de las cuentas.");
        }

        if (!transfers.insertPending(transferId, clientId, key, hash, r)) {
            // Carrera con otra solicitud de la misma clave: el INSERT esperó a que terminara.
            return new Tx1(null, transfers.findByKey(clientId, key).orElseThrow());
        }
        TransferRow pending = transfers.find(transferId).orElseThrow();
        List<Runnable> trail = new ArrayList<>();
        trail.add(() -> audit.record("channel:" + clientId, "TRANSFER_CREATED", AuditWriter.ENTITY_TRANSFER,
                transferId.toString(), correlationId, detail(null, TransferStatus.PENDING, pending, null)));

        RejectionReason rejected = null;
        if (!source.isActive() || (dest != null && !dest.isActive())) {
            rejected = RejectionReason.ACCOUNT_NOT_ACTIVE;
        } else if (!accounts.hold(source.accountId(), r.amount())) {
            boolean stillActive = accounts.find(source.accountId()).map(AccountRow::isActive).orElse(false);
            rejected = stillActive ? RejectionReason.INSUFFICIENT_FUNDS : RejectionReason.ACCOUNT_NOT_ACTIVE;
        }

        TransferRow result;
        if (rejected != null) {
            result = transfers.transition(transferId, TransferStatus.PENDING, TransferStatus.REJECTED, rejected.name());
            events.rejected(result, rejected, correlationId);
            TransferRow row = result;
            RejectionReason reason = rejected;
            trail.add(() -> audit.record("channel:" + clientId, "TRANSFER_REJECTED", AuditWriter.ENTITY_TRANSFER,
                    transferId.toString(), correlationId,
                    detail(TransferStatus.PENDING, TransferStatus.REJECTED, row, reason.name())));
        } else {
            sagas.start(transferId, "FUNDS_HELD", resumeAfter);
            events.initiated(pending, correlationId);
            result = pending;
        }
        trail.forEach(Runnable::run);   // auditoría al final: ver AuditWriter
        return new Tx1(result, null);
    }

    private AccountRow customerAccount(UUID accountId) {
        return accounts.find(accountId)
                .filter(AccountRow::isCustomer)
                .orElseThrow(() -> new ApiException(HttpStatus.NOT_FOUND, "account-not-found", "Cuenta inexistente",
                        "La cuenta " + accountId + " no existe."));
    }

    // ---------------------------------------------------------------- score + tx2

    private TransferRow close(TransferRow pending, UUID correlationId) {
        ScoreResult score = scorer.score(new ScoreRequest(pending.transferId(), pending.sourceAccountId().toString(),
                pending.destAccountId() != null ? pending.destAccountId().toString() : pending.destExternalRef(),
                pending.amount(), pending.currency(), pending.transferType().name(), pending.destCountry(),
                pending.occurredAt()));
        try {
            return tx.execute(status -> applyDecision(pending.transferId(), score, correlationId));
        } catch (DataAccessException | TransactionException e) {
            // Solo fallos de datos: se reintentan. Una violación de invariante (IllegalStateException,
            // etc.) no se disfraza de 503: sale como 500 internal-error.
            log.error("falló el cierre de la transferencia {}; queda PENDING con su retención para reanudarse",
                    pending.transferId(), e);
            try {
                tx.executeWithoutResult(s -> sagas.recordError(pending.transferId(), e.toString()));
            } catch (DataAccessException | TransactionException ignored) {
                // la base puede no estar disponible; el error ya está en el log
            }
            throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "service-unavailable", "Servicio no disponible",
                    "La transferencia no pudo completarse ahora; reintenta con la misma Idempotency-Key.",
                    Map.of("Retry-After", "1"));
        }
    }

    private TransferRow applyDecision(UUID transferId, ScoreResult score, UUID correlationId) {
        TransferRow t = transfers.findForUpdate(transferId).orElseThrow();
        if (t.status() != TransferStatus.PENDING) {
            return t;   // otra solicitud ya la cerró
        }
        transfers.recordScore(transferId, score);
        events.scored(t, score, correlationId);
        List<Runnable> trail = new ArrayList<>();
        TransferRow result = switch (score.decision()) {
            case BLOCK -> releaseAndReject(t, RejectionReason.RISK_BLOCK, correlationId, trail);
            case REVIEW -> {
                TransferRow held = transfers.transition(transferId, TransferStatus.PENDING, TransferStatus.IN_REVIEW, null);
                sagas.advance(transferId, "IN_REVIEW", "IN_REVIEW");
                events.held(held, correlationId);
                trail.add(() -> audit.record(SYSTEM_ACTOR, "TRANSFER_HELD", AuditWriter.ENTITY_TRANSFER,
                        transferId.toString(), correlationId,
                        detail(TransferStatus.PENDING, TransferStatus.IN_REVIEW, held, "FRAUD_REVIEW")));
                yield held;
            }
            case APPROVE -> t.rail() == Rail.INTERNAL
                    ? settleInternal(t, correlationId, trail)
                    : awaitRail(t, correlationId, trail);
        };
        trail.forEach(Runnable::run);   // auditoría al final: ver AuditWriter
        return result;
    }

    private TransferRow settleInternal(TransferRow t, UUID correlationId, List<Runnable> trail) {
        List<AccountRow> locked = accounts.lockInOrder(t.sourceAccountId(), t.destAccountId());
        boolean allActive = locked.size() == 2 && locked.stream().allMatch(AccountRow::isActive);
        if (!allActive) {
            return releaseAndReject(t, RejectionReason.ACCOUNT_NOT_ACTIVE, correlationId, trail);
        }
        var sourceBalance = accounts.debitHeld(t.sourceAccountId(), t.amount());
        ledger.post(t.transferId(), t.sourceAccountId(), 'D', "TRANSFER", t.amount(), sourceBalance);
        var destBalance = accounts.credit(t.destAccountId(), t.amount());
        ledger.post(t.transferId(), t.destAccountId(), 'C', "TRANSFER", t.amount(), destBalance);

        TransferRow done = transfers.transition(t.transferId(), TransferStatus.PENDING, TransferStatus.COMPLETED, null);
        sagas.advance(t.transferId(), "CREDITED", "COMPLETED");
        events.completed(done, correlationId);
        trail.add(() -> audit.record(SYSTEM_ACTOR, "TRANSFER_COMPLETED", AuditWriter.ENTITY_TRANSFER,
                t.transferId().toString(), correlationId,
                detail(TransferStatus.PENDING, TransferStatus.COMPLETED, done, null)));
        return done;
    }

    /** El rail externo es de la fase 2: la transferencia aprobada espera en PROCESSING con su retención. */
    private TransferRow awaitRail(TransferRow t, UUID correlationId, List<Runnable> trail) {
        TransferRow processing = transfers.transition(t.transferId(), TransferStatus.PENDING,
                TransferStatus.PROCESSING, null);
        sagas.advance(t.transferId(), "AWAITING_RAIL", "WAITING_REPLY");
        trail.add(() -> audit.record(SYSTEM_ACTOR, "TRANSFER_PROCESSING", AuditWriter.ENTITY_TRANSFER,
                t.transferId().toString(), correlationId,
                detail(TransferStatus.PENDING, TransferStatus.PROCESSING, processing, null)));
        return processing;
    }

    private TransferRow releaseAndReject(TransferRow t, RejectionReason reason, UUID correlationId,
            List<Runnable> trail) {
        accounts.release(t.sourceAccountId(), t.amount());
        TransferRow rejected = transfers.transition(t.transferId(), TransferStatus.PENDING, TransferStatus.REJECTED,
                reason.name());
        sagas.advance(t.transferId(), "FUNDS_RELEASED", "COMPENSATED");
        events.rejected(rejected, reason, correlationId);
        trail.add(() -> audit.record(SYSTEM_ACTOR, "TRANSFER_REJECTED", AuditWriter.ENTITY_TRANSFER,
                t.transferId().toString(), correlationId,
                detail(TransferStatus.PENDING, TransferStatus.REJECTED, rejected, reason.name())));
        return rejected;
    }

    // ---------------------------------------------------------------- reintentos

    private Outcome replay(TransferRow existing, String hash, UUID correlationId) {
        if (!existing.requestHash().equals(hash)) {
            throw new ApiException(HttpStatus.UNPROCESSABLE_ENTITY, "idempotency-key-reuse",
                    "Idempotency-Key reutilizada",
                    "La Idempotency-Key ya se usó con un cuerpo distinto.");
        }
        if (existing.status() != TransferStatus.PENDING) {
            return new Outcome(TransferView.of(existing), true);
        }
        Boolean claimed = tx.execute(s -> sagas.claimStalled(existing.transferId(), resumeAfter));
        if (!Boolean.TRUE.equals(claimed)) {
            throw new ApiException(HttpStatus.CONFLICT, "idempotency-in-progress", "Solicitud en curso",
                    "La solicitud original con esta Idempotency-Key aún está en curso.",
                    Map.of("Retry-After", "1"));
        }
        log.info("reanudando la transferencia {} detenida en PENDING", existing.transferId());
        TransferRow closed = close(existing, correlationId);
        return new Outcome(TransferView.of(closed), true);
    }

    private static ApiException rejection(RejectionReason reason) {
        return switch (reason) {
            case INSUFFICIENT_FUNDS -> new ApiException(HttpStatus.UNPROCESSABLE_ENTITY, "insufficient-funds",
                    "Saldo insuficiente", "El saldo disponible es menor que el monto.");
            case ACCOUNT_NOT_ACTIVE -> new ApiException(HttpStatus.UNPROCESSABLE_ENTITY, "account-not-active",
                    "Cuenta no activa", "La cuenta de origen o de destino está bloqueada o cerrada.");
            case RISK_BLOCK, ANALYST_REJECT -> new ApiException(HttpStatus.UNPROCESSABLE_ENTITY, "transfer-declined",
                    "Transferencia rechazada", "La transferencia fue rechazada.");
        };
    }

    private static Map<String, Object> detail(TransferStatus from, TransferStatus to, TransferRow t, String reason) {
        Map<String, Object> d = new LinkedHashMap<>();
        d.put("from_status", from == null ? null : from.name());
        d.put("to_status", to.name());
        d.put("client_id", t.clientId());
        d.put("idempotency_key", t.idempotencyKey().toString());
        d.put("source_account_id", t.sourceAccountId().toString());
        if (t.destAccountId() != null) {
            d.put("dest_account_id", t.destAccountId().toString());
        }
        d.put("rail", t.rail().name());
        d.put("amount", t.amount().toPlainString());
        d.put("currency", t.currency());
        if (reason != null) {
            d.put("reason", reason);
        }
        return d;
    }
}
