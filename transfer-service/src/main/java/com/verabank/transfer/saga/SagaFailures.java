package com.verabank.transfer.saga;

import com.verabank.transfer.config.VerabankProperties;
import com.verabank.transfer.audit.AuditWriter;
import java.util.Map;
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
    static final String SYSTEM_ACTOR = "system:transfer-service";

    private final SagaRepository sagas;
    private final TransactionTemplate tx;
    private final VerabankProperties.Saga config;
    private final SagaMetrics metrics;
    private final AuditWriter audit;

    public SagaFailures(SagaRepository sagas, PlatformTransactionManager txManager, VerabankProperties properties,
            SagaMetrics metrics, AuditWriter audit) {
        this.sagas = sagas;
        this.metrics = metrics;
        this.audit = audit;
        this.config = properties.saga();
        this.tx = new TransactionTemplate(txManager);
        this.tx.setPropagationBehavior(TransactionDefinition.PROPAGATION_REQUIRES_NEW);
        this.tx.setIsolationLevel(TransactionDefinition.ISOLATION_READ_COMMITTED);
    }

    /** Registra el fallo; nunca lanza (la base puede no estar disponible: el error ya está en el log). */
    public Optional<SagaRepository.Failure> record(UUID sagaId, Throwable error) {
        try {
            Optional<SagaRepository.Failure> failure = tx.execute(s -> {
                Optional<SagaRepository.Failure> f = sagas.recordFailure(sagaId, describe(error),
                        config.backoffBase(), config.backoffMax(), config.maxAttempts());
                f.filter(SagaRepository.Failure::stopped).ifPresent(stopped -> audit.record(SYSTEM_ACTOR,
                        "SAGA_COMPENSATION_FAILED", AuditWriter.ENTITY_TRANSFER, sagaId.toString(),
                        UUID.randomUUID(), Map.of("step", stopped.step(), "attempts", stopped.attempt(),
                                "last_error", describe(error))));
                return f;
            });
            if (failure != null && failure.isPresent()) {
                SagaRepository.Failure f = failure.get();
                if (f.stopped()) {
                    metrics.compensationFailed.increment();
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
