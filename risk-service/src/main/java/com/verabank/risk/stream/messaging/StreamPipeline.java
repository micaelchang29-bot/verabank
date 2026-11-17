package com.verabank.risk.stream.messaging;

import com.verabank.risk.shared.RiskConfig;
import io.smallrye.mutiny.Uni;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import java.util.function.Function;
import org.jboss.logging.Logger;

/**
 * Política de errores común de los dos consumidores: un {@link InvalidMessageException} va directo al {@code .dlt};
 * cualquier otro fallo se reintenta {@code verabank.stream.retries} veces (tres) y luego va al {@code .dlt}. El
 * mensaje solo se reconoce (el offset solo se confirma) cuando el trabajo terminó o el DLT lo recibió.
 */
@ApplicationScoped
public class StreamPipeline {

    private static final Logger LOG = Logger.getLogger(StreamPipeline.class);

    @Inject
    RiskConfig config;
    @Inject
    DeadLetters deadLetters;
    @Inject
    StreamMetrics metrics;

    /** El resultado completa cuando se puede reconocer el mensaje; falla si ni el trabajo ni el DLT lo lograron. */
    public Uni<Void> run(String consumer, SourceRecord record, Function<SourceRecord, Uni<Void>> work) {
        var stream = config.stream();
        Uni<Void> attempt = Uni.createFrom().deferred(() -> {
            metrics.attempt(consumer);
            return work.apply(record);
        });
        return attempt.onFailure(this::retryable)
                .retry().withBackOff(stream.retryBackoff(), stream.retryBackoff()).withJitter(0.0)
                .atMost(stream.retries())
                .onFailure().recoverWithUni(error -> {
                    LOG.debugf(error, "%s: el mensaje %s/%d/%d no se pudo procesar", consumer, record.topic(),
                            record.partition(), record.offset());
                    return deadLetters.send(record, error);
                });
    }

    private boolean retryable(Throwable t) {
        return !(t instanceof InvalidMessageException) && !(t instanceof PublishFailedException);
    }
}
