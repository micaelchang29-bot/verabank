package com.verabank.transfer.transfer;

import com.verabank.transfer.account.AccountRepository;
import com.verabank.transfer.account.AccountRow;
import com.verabank.transfer.audit.AuditWriter;
import com.verabank.transfer.config.VerabankProperties;
import com.verabank.transfer.saga.RailEvents;
import com.verabank.transfer.saga.SagaConflictException;
import com.verabank.transfer.saga.SagaFailures;
import com.verabank.transfer.saga.SagaRepository;
import com.verabank.transfer.saga.SagaStates;
import com.verabank.transfer.scoring.FraudScorer;
import com.verabank.transfer.scoring.ScoreRequest;
import com.verabank.transfer.scoring.ScoreResult;
import com.verabank.transfer.web.ApiException;
import com.verabank.transfer.web.DbErrors;
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
 * Creación de transferencias según §2.3 y ADR-03, y su reanudación (docs/saga.md §2 y §7).
 *
 * <pre>
 * tx1  insertar PENDING (unicidad client_id + clave) · retener fondos · saga RUNNING/FUNDS_HELD
 *      · outbox(TransferInitiated) · auditoría                                           (S1)
 * --   score = FraudScorer.score(req)            (fuera de transacción)
 * tx2  BLOCK: liberar la retención · REJECTED                                            (S2)
 *      REVIEW: IN_REVIEW                                                                 (S3)
 *      APPROVE + INTERNAL: asientos D/C · COMPLETED                                      (S4)
 *      APPROVE + EXTERNAL: PROCESSING · outbox(CreditInstruction) · espera al rail       (S5)
 * </pre>
 *
 * Si tx2 falla, se revierte entera: la transferencia sigue PENDING con su retención (no hay
 * retención huérfana) y el fallo se registra como fallo local de la saga (S14/S15). La saga queda con
 * wake_at vencido tras {@code saga.resume-after}: el sondeo, o un reintento del cliente con la misma
 * clave, la reanudan con el mismo código ({@link #close}); la versión de la saga decide quién cierra
 * tx2 y el perdedor se descarta sin efectos (score idempotente por transfer_id).
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
    private final RailEvents railEvents;
    private final SagaFailures failures;
    private final TransactionTemplate tx;
    private final Duration resumeAfter;
    private final Duration railReplyTimeout;

    public TransferService(TransferRepository transfers, AccountRepository accounts, LedgerRepository ledger,
            SagaRepository sagas, TransferEvents events, AuditWriter audit, FraudScorer scorer,
            RailEvents railEvents, SagaFailures failures, PlatformTransactionManager txManager,
            VerabankProperties properties) {
        this.transfers = transfers;
        this.accounts = accounts;
        this.ledger = ledger;
        this.sagas = sagas;
        this.events = events;
        this.audit = audit;
        this.scorer = scorer;
        this.railEvents = railEvents;
        this.failures = failures;
        this.tx = new TransactionTemplate(txManager);
        this.tx.setIsolationLevel(TransactionDefinition.ISOLATION_READ_COMMITTED);
        this.resumeAfter = properties.saga().resumeAfter();
        this.railReplyTimeout = properties.saga().railReplyTimeout();
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
        TransferRow closed = close(opened, INITIAL_VERSION, correlationId);
        if (closed.status() == TransferStatus.REJECTED) {
            throw rejection(RejectionReason.valueOf(closed.failureReason()));
        }
        return new Outcome(TransferView.of(closed), false);
    }

    /**
     * Decisión del analista sobre una transferencia retenida (S6 y S7). Solo sobre {@code IN_REVIEW}:
     * la transición se condiciona al estado y a la versión de la saga, de modo que con dos analistas a
     * la vez uno recibe 200 y el otro 409. APPROVE reanuda como S4 o S5; REJECT libera la retención.
     * La auditoría {@code TRANSFER_REVIEWED} va en la misma transacción; el actor es el client_id del canal.
     */
    public TransferView review(UUID transferId, ReviewDecision decision, String comment, String actor,
            UUID correlationId) {
        TransferRow reviewed = tx.execute(status -> applyReview(transferId, decision, comment, actor, correlationId));
        return TransferView.of(reviewed);
    }

    private TransferRow applyReview(UUID transferId, ReviewDecision decision, String comment, String actor,
            UUID correlationId) {
        TransferRow current = transfers.find(transferId).orElseThrow(() -> new ApiException(HttpStatus.NOT_FOUND,
                "transfer-not-found", "Transferencia inexistente", "La transferencia " + transferId + " no existe."));
        if (current.status() != TransferStatus.IN_REVIEW) {
            throw reviewNotPending();
        }
        long readVersion = sagas.find(transferId).orElseThrow().version();
        // Compuerta de versión primero (toma el bloqueo de la saga): un segundo analista espera aquí y,
        // al ver la versión cambiada, recibe 409.
        long version = sagas.touch(transferId, readVersion).orElseThrow(TransferService::reviewNotPending);
        TransferRow t = transfers.findForUpdate(transferId).orElseThrow();
        if (t.status() != TransferStatus.IN_REVIEW) {
            throw reviewNotPending();
        }
        List<Runnable> trail = new ArrayList<>();
        TransferRow result = decision == ReviewDecision.APPROVE
                ? approve(t, TransferStatus.IN_REVIEW, version, correlationId, trail)
                : releaseAndReject(t, TransferStatus.IN_REVIEW, RejectionReason.ANALYST_REJECT, version,
                        correlationId, trail);
        trail.add(() -> {
            Map<String, Object> detail = detail(TransferStatus.IN_REVIEW, result.status(), result, null);
            detail.put("decision", decision.name());
            detail.put("comment", comment);
            audit.record(actor, "TRANSFER_REVIEWED", AuditWriter.ENTITY_TRANSFER, transferId.toString(),
                    correlationId, detail);
        });
        trail.forEach(Runnable::run);   // auditoría al final: ver AuditWriter
        return result;
    }

    private static ApiException reviewNotPending() {
        return new ApiException(HttpStatus.CONFLICT, "review-not-pending", "Revisión no pendiente",
                "La transferencia ya no está en revisión.");
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
            sagas.start(transferId, SagaStates.FUNDS_HELD, resumeAfter);
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

    /** Versión de una saga recién creada por tx1. */
    private static final long INITIAL_VERSION = 0;

    /** Cierra tx2 para la solicitud en curso o el reintento del cliente; los fallos de datos son 503. */
    private TransferRow close(TransferRow pending, long version, UUID correlationId) {
        try {
            return closeClaimed(pending, version, correlationId);
        } catch (DataAccessException | TransactionException e) {
            if (!DbErrors.isTransient(e)) {
                throw e;   // permanente: 500 internal-error; un reintento repetiría el fallo
            }
            throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "service-unavailable", "Servicio no disponible",
                    "La transferencia no pudo completarse ahora; reintenta con la misma Idempotency-Key.",
                    Map.of("Retry-After", "1"));
        }
    }

    /**
     * Reanudación del sondeo (RUNNING / FUNDS_HELD): la saga ya está reclamada con {@code claimedVersion}.
     * Comparte el código con el reintento del cliente. Un fallo queda registrado como fallo local y se
     * relanza; si otro actor ganó la reclamación, el trabajo se descarta sin efectos.
     */
    public void resumeClaimed(UUID transferId, long claimedVersion) {
        TransferRow pending = transfers.find(transferId).orElseThrow();
        if (pending.status() != TransferStatus.PENDING) {
            return;
        }
        log.info("el sondeo reanuda la transferencia {} detenida tras la retención", transferId);
        closeClaimed(pending, claimedVersion, UUID.randomUUID());
    }

    /**
     * Score fuera de transacción y tx2 con la versión reclamada. Cualquier fallo se registra como fallo
     * local (S14/S15) con tx2 ya revertida, y se relanza sin traducir.
     */
    private TransferRow closeClaimed(TransferRow pending, long version, UUID correlationId) {
        try {
            ScoreResult score = scorer.score(new ScoreRequest(pending.transferId(),
                    pending.sourceAccountId().toString(),
                    pending.destAccountId() != null ? pending.destAccountId().toString() : pending.destExternalRef(),
                    pending.amount(), pending.currency(), pending.transferType().name(), pending.destCountry(),
                    pending.occurredAt()));
            return tx.execute(status -> applyDecision(pending.transferId(), version, score, correlationId));
        } catch (SagaConflictException e) {
            log.info("la transferencia {} la cerró otro actor (versión de la saga distinta); se descarta", 
                    pending.transferId());
            return transfers.find(pending.transferId()).orElseThrow();
        } catch (RuntimeException e) {
            // tx2 se revirtió entera: la transferencia sigue PENDING con su retención, para reanudarse.
            log.error("falló el cierre de la transferencia {}; queda PENDING con su retención para reanudarse",
                    pending.transferId(), e);
            failures.record(pending.transferId(), e);
            throw e;
        }
    }

    private TransferRow applyDecision(UUID transferId, long claimedVersion, ScoreResult score, UUID correlationId) {
        long version = sagas.touch(transferId, claimedVersion)
                .orElseThrow(() -> new SagaConflictException("saga " + transferId + " ya no está en " + claimedVersion));
        TransferRow t = transfers.findForUpdate(transferId).orElseThrow();
        if (t.status() != TransferStatus.PENDING) {
            return t;   // otra solicitud ya la cerró
        }
        transfers.recordScore(transferId, score);
        events.scored(t, score, correlationId);
        List<Runnable> trail = new ArrayList<>();
        TransferRow result = switch (score.decision()) {
            case BLOCK -> releaseAndReject(t, TransferStatus.PENDING, RejectionReason.RISK_BLOCK, version,
                    correlationId, trail);
            case REVIEW -> {
                TransferRow held = transfers.transition(transferId, TransferStatus.PENDING, TransferStatus.IN_REVIEW, null);
                advance(transferId, version, SagaStates.IN_REVIEW, SagaStates.IN_REVIEW, null);
                events.held(held, correlationId);
                trail.add(() -> audit.record(SYSTEM_ACTOR, "TRANSFER_HELD", AuditWriter.ENTITY_TRANSFER,
                        transferId.toString(), correlationId,
                        detail(TransferStatus.PENDING, TransferStatus.IN_REVIEW, held, "FRAUD_REVIEW")));
                yield held;
            }
            case APPROVE -> approve(t, TransferStatus.PENDING, version, correlationId, trail);
        };
        trail.forEach(Runnable::run);   // auditoría al final: ver AuditWriter
        return result;
    }

    /** APPROVE del scorer o del analista: S4 (INTERNAL) o S5 (EXTERNAL). */
    private TransferRow approve(TransferRow t, TransferStatus from, long version, UUID correlationId,
            List<Runnable> trail) {
        return t.rail() == Rail.INTERNAL
                ? settleInternal(t, from, version, correlationId, trail)
                : awaitRail(t, from, version, correlationId, trail);
    }

    private void advance(UUID transferId, long version, String step, String status, Duration wakeIn) {
        if (!sagas.advance(transferId, version, step, status, wakeIn, null)) {
            throw new SagaConflictException("saga " + transferId + " ya no está en la versión " + version);
        }
    }

    private TransferRow settleInternal(TransferRow t, TransferStatus from, long version, UUID correlationId,
            List<Runnable> trail) {
        List<AccountRow> locked = accounts.lockInOrder(t.sourceAccountId(), t.destAccountId());
        boolean allActive = locked.size() == 2 && locked.stream().allMatch(AccountRow::isActive);
        if (!allActive) {
            return releaseAndReject(t, from, RejectionReason.ACCOUNT_NOT_ACTIVE, version, correlationId, trail);
        }
        var sourceBalance = accounts.debitHeld(t.sourceAccountId(), t.amount());
        ledger.post(t.transferId(), t.sourceAccountId(), 'D', "TRANSFER", t.amount(), sourceBalance);
        var destBalance = accounts.credit(t.destAccountId(), t.amount());
        ledger.post(t.transferId(), t.destAccountId(), 'C', "TRANSFER", t.amount(), destBalance);

        TransferRow done = transfers.transition(t.transferId(), from, TransferStatus.COMPLETED, null);
        advance(t.transferId(), version, SagaStates.DONE, SagaStates.COMPLETED, null);
        events.completed(done, correlationId);
        trail.add(() -> audit.record(SYSTEM_ACTOR, "TRANSFER_COMPLETED", AuditWriter.ENTITY_TRANSFER,
                t.transferId().toString(), correlationId, detail(from, TransferStatus.COMPLETED, done, null)));
        return done;
    }

    /** S5: la instrucción de abono sale por el outbox y la saga espera la respuesta del rail. */
    private TransferRow awaitRail(TransferRow t, TransferStatus from, long version, UUID correlationId,
            List<Runnable> trail) {
        TransferRow processing = transfers.transition(t.transferId(), from, TransferStatus.PROCESSING, null);
        advance(t.transferId(), version, SagaStates.CREDIT_REQUESTED, SagaStates.WAITING_REPLY, railReplyTimeout);
        railEvents.creditInstruction(processing, correlationId);
        trail.add(() -> audit.record(SYSTEM_ACTOR, "TRANSFER_PROCESSING", AuditWriter.ENTITY_TRANSFER,
                t.transferId().toString(), correlationId, detail(from, TransferStatus.PROCESSING, processing, null)));
        return processing;
    }

    private TransferRow releaseAndReject(TransferRow t, TransferStatus from, RejectionReason reason, long version,
            UUID correlationId, List<Runnable> trail) {
        accounts.release(t.sourceAccountId(), t.amount());
        TransferRow rejected = transfers.transition(t.transferId(), from, TransferStatus.REJECTED, reason.name());
        advance(t.transferId(), version, SagaStates.DONE, SagaStates.COMPENSATED, null);
        events.rejected(rejected, reason, correlationId);
        trail.add(() -> audit.record(SYSTEM_ACTOR, "TRANSFER_REJECTED", AuditWriter.ENTITY_TRANSFER,
                t.transferId().toString(), correlationId, detail(from, TransferStatus.REJECTED, rejected, reason.name())));
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
        Optional<Long> claimed = tx.execute(s -> sagas.claim(existing.transferId(), resumeAfter));
        if (claimed == null || claimed.isEmpty()) {
            throw new ApiException(HttpStatus.CONFLICT, "idempotency-in-progress", "Solicitud en curso",
                    "La solicitud original con esta Idempotency-Key aún está en curso.",
                    Map.of("Retry-After", Long.toString(retryAfterSeconds(existing.transferId()))));
        }
        log.info("reanudando la transferencia {} detenida en PENDING", existing.transferId());
        TransferRow closed = close(existing, claimed.get(), correlationId);
        return new Outcome(TransferView.of(closed), true);
    }

    static final long RETRY_AFTER_MIN_SECONDS = 1;
    static final long RETRY_AFTER_MAX_SECONDS = 5;

    /**
     * Cuándo tiene sentido reintentar: al vencer el wake_at de la saga, que es cuando un
     * reintento puede reanudarla. {@code clamp(wake_at - ahora, 1, 5)} segundos: el tope
     * evita pedir esperas largas a quien reintenta una solicitud que puede estar a punto de
     * terminar; el mínimo, que un wake_at ausente o a punto de vencer dé 1 s.
     */
    private long retryAfterSeconds(UUID transferId) {
        long remaining = tx.execute(s -> sagas.secondsUntilWake(transferId).orElse(RETRY_AFTER_MIN_SECONDS));
        return clampRetryAfter(remaining);
    }

    static long clampRetryAfter(long remainingSeconds) {
        return Math.min(RETRY_AFTER_MAX_SECONDS, Math.max(RETRY_AFTER_MIN_SECONDS, remainingSeconds));
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
        return TransferAudit.detail(from, to, t, reason);
    }
}
