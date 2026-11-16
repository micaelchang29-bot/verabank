package com.verabank.transfer.saga;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import java.time.Duration;
import org.springframework.stereotype.Component;

/** Métricas de docs/saga.md §8 que no son gauges (los gauges los registra {@link SagaGauges}). */
@Component
public class SagaMetrics {

    public final Counter compensationFailed;
    public final Counter conflictingReply;
    public final Counter inboxDuplicates;
    public final Counter inboxDlt;
    public final Counter outboxDlt;
    public final Timer compensationDuration;

    public SagaMetrics(MeterRegistry meters) {
        compensationFailed = Counter.builder("saga.compensation_failed")
                .description("Pasos de la saga a COMPENSATION_FAILED").register(meters);
        conflictingReply = Counter.builder("saga.conflicting_reply")
                .description("Respuestas del rail que contradicen el estado final de la saga").register(meters);
        inboxDuplicates = Counter.builder("inbox.duplicates")
                .description("Respuestas repetidas del rail, sin efectos").register(meters);
        inboxDlt = Counter.builder("inbox.dlt")
                .description("Mensajes de rail.replies.v1 enviados al DLT").register(meters);
        outboxDlt = Counter.builder("outbox.dlt")
                .description("Eventos del outbox enviados al DLT").register(meters);
        compensationDuration = Timer.builder("saga.compensation.duration")
                .description("De recibir el rechazo o la cancelación a COMPENSATED").register(meters);
    }

    public void compensated(Duration elapsed) {
        compensationDuration.record(elapsed.isNegative() ? Duration.ZERO : elapsed);
    }
}
