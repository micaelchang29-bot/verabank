package com.verabank.transfer.saga;

import com.verabank.transfer.config.VerabankProperties;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import java.util.Optional;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * Fallos locales de un paso (S14) y su escalada a COMPENSATION_FAILED (S15), docs/saga.md §2.
 *
 * <p>Se registra en una transacción propia ({@code REQUIRES_NEW}) y siempre después de que la
 * transacción del paso se haya revertido: así el bloqueo de la saga ya está libre y el contador no
 * se pierde con el rollback. Los fondos no se tocan: siguen retenidos y {@code transfers.status}
 * no cambia.
 */
@Component
public class SagaFailures {

    private static final Logger log = LoggerFactory.getLogger(SagaFailures.class);

    private final SagaRepository sagas;
    private final TransactionTemplate tx;
    private final VerabankProperties.Saga config;
    private final Counter compensationFailed;

    public SagaFailures(SagaRepository sagas, PlatformTransactionManager txManager, VerabankProperties properties,
            MeterRegistry meters) {
        this.sagas = sagas;
        this.config = properties.saga();
        this.tx = new TransactionTemplate(txManager);
        this.tx.setPropagationBehavior(TransactionDefinition.PROPAGATION_REQUIRES_NEW);
        this.tx.setIsolationLevel(TransactionDefinition.ISOLATION_READ_COMMITTED);
        this.compensationFailed = Counter.builder("saga.compensation_failed")
                .description("Pasos de la saga a COMPENSATION_FAILED").register(meters);
    }

    /** Registra el fallo; nunca lanza (la base puede no estar disponible: el error ya está en el log). */
    public Optional<SagaRepository.Failure> record(UUID sagaId, Throwable error) {
        try {
            Optional<SagaRepository.Failure> failure = tx.execute(s -> sagas.recordFailure(sagaId, describe(error),
                    config.backoffBase(), config.backoffMax(), config.maxAttempts()));
            if (failure != null && failure.isPresent()) {
                SagaRepository.Failure f = failure.get();
                if (f.stopped()) {
                    compensationFailed.increment();
                    log.error("la saga {} pasa a COMPENSATION_FAILED en el paso {} tras {} fallos locales; los fondos "
                            + "siguen retenidos y requiere un operador (POST /internal/v1/sagas/{}/resume)",
                            sagaId, f.step(), f.attempt(), sagaId);
                } else {
                    log.warn("fallo local {} de {} en la saga {} (paso {}); se reintenta con backoff: {}",
                            f.attempt(), config.maxAttempts(), sagaId, f.step(), describe(error));
                }
            }
            return failure == null ? Optional.empty() : failure;
        } catch (RuntimeException e) {
            log.error("no se pudo registrar el fallo de la saga {}: {} (fallo original: {})", sagaId,
                    e.getMessage(), describe(error));
            return Optional.empty();
        }
    }

    private static String describe(Throwable error) {
        return error.toString();
    }
}
