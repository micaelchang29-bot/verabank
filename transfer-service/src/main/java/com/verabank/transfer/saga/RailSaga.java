package com.verabank.transfer.saga;

import com.verabank.transfer.account.AccountRepository;
import com.verabank.transfer.account.AccountRow;
import com.verabank.transfer.audit.AuditWriter;
import com.verabank.transfer.config.VerabankProperties;
import com.verabank.transfer.transfer.LedgerRepository;
import com.verabank.transfer.transfer.TransferAudit;
import com.verabank.transfer.transfer.TransferEvents;
import com.verabank.transfer.transfer.TransferRepository;
import com.verabank.transfer.transfer.TransferRow;
import com.verabank.transfer.transfer.TransferStatus;
import java.time.Duration;
import java.time.Instant;
import java.util.Map;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * Tramo externo de la saga (docs/saga.md §2, S8 a S10 y §5). Cada método corre dentro de la
 * transacción de quien lo llama y empieza por la escritura condicionada a la versión de la saga
 * (que toma su bloqueo): el orden de bloqueos es siempre saga, transferencia, cuentas, auditoría.
 */
@Component
public class RailSaga {

    private static final Logger log = LoggerFactory.getLogger(RailSaga.class);
    static final String SYSTEM_ACTOR = "system:transfer-service";

    /** Qué hizo una respuesta del rail; el llamador cuenta las métricas tras confirmar la transacción. */
    public enum Result {
        /** Completó la transferencia (S8). */
        COMPLETED,
        /** Inició la compensación (S9); falta liberar la retención (S10). */
        COMPENSATION_STARTED,
        /** Repetida con otro event_id y misma conclusión: sin cambios. */
        DUPLICATE,
        /** Contradice el estado final de la saga: sin cambios, a conciliar a mano. */
        CONFLICTING,
        /** Sin efecto (UNKNOWN, o rechazo repetido durante la compensación). */
        NO_CHANGE
    }

    private final TransferRepository transfers;
    private final AccountRepository accounts;
    private final LedgerRepository ledger;
    private final SagaRepository sagas;
    private final TransferEvents events;
    private final AuditWriter audit;
    private final SagaMetrics metrics;
    private final SagaFailures failures;
    private final RailEvents railEvents;
    private final VerabankProperties.Saga config;
    private final TransactionTemplate tx;

    public RailSaga(TransferRepository transfers, AccountRepository accounts, LedgerRepository ledger,
            SagaRepository sagas, TransferEvents events, AuditWriter audit, SagaMetrics metrics,
            SagaFailures failures, RailEvents railEvents, VerabankProperties properties,
            PlatformTransactionManager txManager) {
        this.transfers = transfers;
        this.accounts = accounts;
        this.ledger = ledger;
        this.sagas = sagas;
        this.events = events;
        this.audit = audit;
        this.metrics = metrics;
        this.failures = failures;
        this.railEvents = railEvents;
        this.config = properties.saga();
        this.tx = new TransactionTemplate(txManager);
        this.tx.setIsolationLevel(TransactionDefinition.ISOLATION_READ_COMMITTED);
    }

    /** Aplica una respuesta del rail según la tabla de docs/saga.md §5. */
    public Result onReply(RailReply reply, SagaRow saga, TransferRow transfer) {
        return switch (saga.status()) {
            case SagaStates.WAITING_REPLY -> switch (reply.kind()) {
                case CONFIRM -> {
                    complete(saga, transfer, reply);
                    yield Result.COMPLETED;
                }
                case REJECT -> {
                    startCompensation(saga, transfer, reply);
                    yield Result.COMPENSATION_STARTED;
                }
                case UNKNOWN -> Result.NO_CHANGE;
            };
            case SagaStates.COMPLETED -> switch (reply.kind()) {
                case CONFIRM -> Result.DUPLICATE;
                case REJECT -> Result.CONFLICTING;
                case UNKNOWN -> Result.NO_CHANGE;
            };
            case SagaStates.COMPENSATING, SagaStates.COMPENSATED -> contradictsCompensation(reply);
            case SagaStates.COMPENSATION_FAILED -> {
                // docs/saga.md §5: se distingue por el estado en que se detuvo (payload.resume_status).
                if (SagaStates.COMPENSATING.equals(saga.payloadText("resume_status"))) {
                    yield contradictsCompensation(reply);   // ya se compensaba: un CONFIRMED contradice
                }
                // Se detuvo antes de compensar, esperando al rail: la respuesta no es una contradicción. Se ignora
                // y, al reanudarla, el sondeo vuelve a preguntar al rail y la resuelve.
                log.warn("respuesta {} ({}) para la saga {} detenida en COMPENSATION_FAILED antes de compensar "
                        + "(resume_status {}); se ignora y la resolverá el sondeo tras la reanudación",
                        reply.eventType(), reply.kind(), saga.sagaId(), saga.payloadText("resume_status"));
                yield Result.NO_CHANGE;
            }
            default -> throw new InvalidReplyException("la saga " + saga.sagaId() + " está en " + saga.status()
                    + "/" + saga.currentStep() + ": aún no se emitió la instrucción al rail");
        };
    }

    private static Result contradictsCompensation(RailReply reply) {
        return reply.kind() == ReplyKind.CONFIRM ? Result.CONFLICTING : Result.NO_CHANGE;
    }

    /**
     * S8: el rail confirmó el abono. Asiento D en el origen y C en CLEARING, la retención se consume,
     * COMPLETED. La escritura condicionada a la versión va primero: un perdedor no deja efectos.
     */
    public void complete(SagaRow saga, TransferRow t, RailReply reply) {
        transitionOrConflict(saga, SagaStates.DONE, SagaStates.COMPLETED, null, Map.of());
        TransferRow locked = transfers.findForUpdate(t.transferId()).orElseThrow();
        require(locked, TransferStatus.PROCESSING);
        AccountRow clearing = accounts.findClearing(t.currency()).orElseThrow(
                () -> new IllegalStateException("no hay cuenta CLEARING en " + t.currency()));
        accounts.lockInOrder(t.sourceAccountId(), clearing.accountId());
        var sourceBalance = accounts.debitHeld(t.sourceAccountId(), t.amount());
        ledger.post(t.transferId(), t.sourceAccountId(), 'D', "TRANSFER", t.amount(), sourceBalance);
        var clearingBalance = accounts.credit(clearing.accountId(), t.amount());
        ledger.post(t.transferId(), clearing.accountId(), 'C', "TRANSFER", t.amount(), clearingBalance);

        TransferRow done = transfers.transition(t.transferId(), TransferStatus.PROCESSING, TransferStatus.COMPLETED,
                null);
        events.completed(done, reply.correlationId());
        audit.record(SYSTEM_ACTOR, "TRANSFER_COMPLETED", AuditWriter.ENTITY_TRANSFER, t.transferId().toString(),
                reply.correlationId(), withEvent(TransferAudit.detail(TransferStatus.PROCESSING,
                        TransferStatus.COMPLETED, done, null), reply));
    }

    /** S9: el rail rechazó o canceló. Se registra el inicio de la compensación; S10 va en otra transacción. */
    public void startCompensation(SagaRow saga, TransferRow t, RailReply reply) {
        transitionOrConflict(saga, SagaStates.COMPENSATING, SagaStates.COMPENSATING, Duration.ZERO,
                Map.of("compensation_started_at", Instant.now().toString(), "fail_reason", reply.failReason()));
        audit.record(SYSTEM_ACTOR, "SAGA_COMPENSATION_STARTED", AuditWriter.ENTITY_TRANSFER,
                t.transferId().toString(), reply.correlationId(),
                withEvent(TransferAudit.detail(TransferStatus.PROCESSING, TransferStatus.PROCESSING, t,
                        reply.failReason()), reply));
    }

    /**
     * S10 en su propia transacción, justo después de S9. Si falla, S9 no se revierte: la saga sigue
     * COMPENSATING, el fallo cuenta como fallo local (S14) y la reintenta el sondeo.
     */
    public void compensateNow(UUID transferId, UUID correlationId) {
        try {
            tx.executeWithoutResult(s -> {
                SagaRow saga = sagas.findForUpdate(transferId).orElseThrow();
                if (SagaStates.COMPENSATING.equals(saga.status())) {
                    compensate(saga, correlationId);
                }
            });
        } catch (RuntimeException e) {
            log.error("falló la liberación de la retención de {} (S10); la saga sigue COMPENSATING", transferId, e);
            failures.record(transferId, e);
        }
    }

    /** S10: liberar la retención, FAILED con su causa y TransferFailed {@code compensated = true}. */
    public void compensate(SagaRow saga, UUID correlationId) {
        UUID id = saga.sagaId();
        String reason = saga.payloadText("fail_reason");
        Instant startedAt = saga.payloadText("compensation_started_at") == null ? null
                : Instant.parse(saga.payloadText("compensation_started_at"));
        transitionOrConflict(saga, SagaStates.DONE, SagaStates.COMPENSATED, null, Map.of());
        TransferRow t = transfers.findForUpdate(id).orElseThrow();
        require(t, TransferStatus.PROCESSING);
        accounts.release(t.sourceAccountId(), t.amount());
        TransferRow failed = transfers.transition(id, TransferStatus.PROCESSING, TransferStatus.FAILED, reason);
        events.failed(failed, reason, correlationId);
        audit.record(SYSTEM_ACTOR, "TRANSFER_FAILED", AuditWriter.ENTITY_TRANSFER, id.toString(), correlationId,
                TransferAudit.detail(TransferStatus.PROCESSING, TransferStatus.FAILED, failed, reason));
        if (startedAt != null) {
            afterCommit(() -> metrics.compensated(Duration.between(startedAt, Instant.now())));
        }
    }

    /**
     * El rail no respondió a tiempo (S11 a S13). Pregunta por el estado del abono y, tras
     * {@code saga.cancel-after-polls} consultas sin respuesta concluyente, pide la cancelación y la
     * repite con backoff. Nunca compensa: solo una respuesta explícita del rail lo hace, y un rail
     * callado no consume {@code attempt}.
     */
    public void pollRail(SagaRow saga) {
        TransferRow t = transfers.find(saga.sagaId()).orElseThrow();
        require(t, TransferStatus.PROCESSING);
        UUID correlationId = UUID.randomUUID();
        if (SagaStates.CREDIT_REQUESTED.equals(saga.currentStep())) {
            long polls = saga.payloadLong("polls");
            if (polls < config.cancelAfterPolls()) {
                // S11: la espera ya se cuenta con polls incrementado.
                transitionOrConflict(saga, SagaStates.CREDIT_REQUESTED, SagaStates.WAITING_REPLY,
                        backoff(config.pollBackoffBase(), config.pollBackoffMax(), polls + 1),
                        Map.of("polls", polls + 1));
                railEvents.creditStatusQuery(t, correlationId);
            } else {
                // S12: sin respuesta concluyente tras las consultas, se pide la cancelación.
                transitionOrConflict(saga, SagaStates.CANCEL_REQUESTED, SagaStates.WAITING_REPLY,
                        config.pollBackoffBase(), Map.of("cancels", 1));
                railEvents.creditCancel(t, correlationId);
                audit.record(SYSTEM_ACTOR, "SAGA_CANCEL_REQUESTED", AuditWriter.ENTITY_TRANSFER,
                        t.transferId().toString(), correlationId,
                        TransferAudit.detail(TransferStatus.PROCESSING, TransferStatus.PROCESSING, t,
                                "RAIL_SILENT"));
            }
        } else if (SagaStates.CANCEL_REQUESTED.equals(saga.currentStep())) {
            long cancels = saga.payloadLong("cancels") + 1;   // S13
            transitionOrConflict(saga, SagaStates.CANCEL_REQUESTED, SagaStates.WAITING_REPLY,
                    backoff(config.pollBackoffBase(), config.pollBackoffMax(), cancels), Map.of("cancels", cancels));
            railEvents.creditCancel(t, correlationId);
        } else {
            throw new IllegalStateException("paso de espera desconocido: " + saga.currentStep());
        }
    }

    /** {@code min(base · 2^(n−1), max)}. */
    static Duration backoff(Duration base, Duration max, long n) {
        Duration d = base.multipliedBy(1L << Math.min(Math.max(n - 1, 0), 30));
        return d.compareTo(max) > 0 ? max : d;
    }

    private void transitionOrConflict(SagaRow saga, String step, String status, Duration wakeIn,
            Map<String, ?> patch) {
        if (!sagas.advance(saga.sagaId(), saga.version(), step, status, wakeIn, patch)) {
            throw new SagaConflictException("la saga " + saga.sagaId() + " ya no está en la versión "
                    + saga.version());
        }
    }

    private static void require(TransferRow t, TransferStatus expected) {
        if (t.status() != expected) {
            throw new IllegalStateException("la transferencia " + t.transferId() + " está en " + t.status()
                    + ", se esperaba " + expected);
        }
    }

    private static Map<String, Object> withEvent(Map<String, Object> detail, RailReply reply) {
        detail.put("rail_event_id", reply.eventId().toString());
        detail.put("rail_event_type", reply.eventType());
        return detail;
    }

    static void afterCommit(Runnable action) {
        if (TransactionSynchronizationManager.isSynchronizationActive()) {
            TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
                @Override
                public void afterCommit() {
                    action.run();
                }
            });
        } else {
            action.run();
        }
    }
}
