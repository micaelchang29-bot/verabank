package com.verabank.transfer.saga;

import com.verabank.transfer.config.VerabankProperties;
import com.verabank.transfer.transfer.TransferService;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicLong;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * Sondeo de la saga (docs/saga.md §7): cada {@code saga.poller-interval} toma hasta
 * {@code saga.poller-batch} sagas con {@code wake_at} vencido, una transacción por saga y
 * {@code FOR UPDATE SKIP LOCKED}: varias instancias reparten el trabajo sin pisarse.
 *
 * <p>Los pasos con llamada externa (RUNNING / FUNDS_HELD: pedir el score) usan arrendamiento: una
 * transacción corta reclama la saga empujando {@code wake_at} y subiendo la versión; el scorer se
 * llama fuera de transacción y tx2 se cierra con la versión reclamada, con el mismo código que usa
 * el reintento del cliente con la misma clave.
 *
 * <p>Un fallo del paso revierte su transacción y se registra después, en otra (S14/S15): así el
 * contador de intentos no se pierde con el rollback ni choca con el bloqueo de la saga.
 */
@Component
@ConditionalOnProperty(prefix = "verabank.saga", name = "poller-enabled", havingValue = "true",
        matchIfMissing = true)
public class SagaPoller {

    private static final Logger log = LoggerFactory.getLogger(SagaPoller.class);

    private final SagaRepository sagas;
    private final RailSaga railSaga;
    private final TransferService transferService;
    private final SagaFailures failures;
    private final TransactionTemplate tx;
    private final VerabankProperties.Saga config;
    private final AtomicLong handled = new AtomicLong();

    public SagaPoller(SagaRepository sagas, RailSaga railSaga, TransferService transferService,
            SagaFailures failures, PlatformTransactionManager txManager, VerabankProperties properties) {
        this.sagas = sagas;
        this.railSaga = railSaga;
        this.transferService = transferService;
        this.failures = failures;
        this.config = properties.saga();
        this.tx = new TransactionTemplate(txManager);
        this.tx.setIsolationLevel(TransactionDefinition.ISOLATION_READ_COMMITTED);
    }

    /** Sagas atendidas por esta instancia desde que arrancó. */
    public long handled() {
        return handled.get();
    }

    @Scheduled(fixedDelayString = "${verabank.saga.poller-interval:1s}")
    public void poll() {
        try {
            tick();
        } catch (RuntimeException e) {
            // Base de datos caída u otro error transitorio: el próximo sondeo lo reintenta.
            log.warn("sondeo de la saga fallido: {}", e.getMessage());
        }
    }

    /** Un lote del sondeo; devuelve cuántas sagas atendió. */
    public int tick() {
        int done = 0;
        while (done < config.pollerBatch()) {
            Step step;
            try {
                step = tx.execute(s -> pickAndRun());
            } catch (StepFailed failed) {
                log.error("falló el paso de la saga {}", failed.sagaId, failed.getCause());
                handled.incrementAndGet();
                done++;
                // La transacción del paso ya se revirtió: el fallo se registra en otra.
                if (failures.record(failed.sagaId, failed.getCause()).isEmpty()) {
                    break;   // no se pudo registrar: se reintenta en el próximo sondeo
                }
                continue;
            }
            if (step == null) {
                break;
            }
            handled.incrementAndGet();
            done++;
            if (step.claimedVersion != null) {
                try {
                    transferService.resumeClaimed(step.sagaId, step.claimedVersion);
                } catch (RuntimeException e) {
                    log.warn("la reanudación de {} falló; ya quedó registrada como fallo local: {}", step.sagaId,
                            e.toString());
                }
            }
        }
        return done;
    }

    private record Step(UUID sagaId, Long claimedVersion) {
    }

    private static final class StepFailed extends RuntimeException {
        final transient UUID sagaId;

        StepFailed(UUID sagaId, Throwable cause) {
            super(cause);
            this.sagaId = sagaId;
        }
    }

    /** Dentro de la transacción de la saga: toma la siguiente vencida y ejecuta su paso. */
    private Step pickAndRun() {
        Optional<SagaRow> due = sagas.nextDue(List.of(SagaStates.RUNNING, SagaStates.COMPENSATING));
        if (due.isEmpty()) {
            return null;
        }
        SagaRow saga = due.get();
        try {
            return switch (saga.status()) {
                case SagaStates.RUNNING -> {
                    // FUNDS_HELD: reclamar con arrendamiento; el score y tx2 van fuera de esta transacción.
                    Optional<Long> version = sagas.claim(saga.sagaId(), config.resumeAfter());
                    yield new Step(saga.sagaId(), version.orElse(null));
                }
                case SagaStates.COMPENSATING -> {
                    railSaga.compensate(saga, UUID.randomUUID());   // S10
                    yield new Step(saga.sagaId(), null);
                }
                default -> throw new IllegalStateException("estado no atendido por el sondeo: " + saga.status());
            };
        } catch (RuntimeException e) {
            throw new StepFailed(saga.sagaId(), e);
        }
    }
}
