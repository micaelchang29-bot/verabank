package com.verabank.transfer.saga;

import com.verabank.transfer.config.Sql;
import com.verabank.transfer.config.VerabankProperties;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.MeterRegistry;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * Gauges de docs/saga.md §8. Se calculan con una consulta programada cada {@code saga.stuck-refresh}
 * (15 s), no en cada lectura de {@code /actuator/metrics}: leer una métrica no toca la base de datos.
 *
 * <ul>
 *   <li>{@code saga.stuck{reason=compensation_failed}}: sagas en COMPENSATION_FAILED.</li>
 *   <li>{@code saga.stuck{reason=no_progress}}: sagas ni finales ni IN_REVIEW con {@code updated_at} más
 *       viejo que {@code saga.stuck-after}. Mientras alguno sea mayor que 0 se registra un ERROR con los ids.</li>
 *   <li>{@code outbox.pending} y {@code outbox.oldest.age}: eventos sin publicar y edad en segundos del más viejo.</li>
 * </ul>
 */
@Component
public class SagaGauges {

    private static final Logger log = LoggerFactory.getLogger(SagaGauges.class);

    private final SagaRepository sagas;
    private final JdbcClient jdbc;
    private final Duration stuckAfter;
    private final AtomicLong compensationFailed = new AtomicLong();
    private final AtomicLong noProgress = new AtomicLong();
    private final AtomicLong outboxPending = new AtomicLong();
    private final AtomicReference<Instant> outboxOldest = new AtomicReference<>();

    public SagaGauges(SagaRepository sagas, JdbcClient jdbc, VerabankProperties properties, MeterRegistry meters) {
        this.sagas = sagas;
        this.jdbc = jdbc;
        this.stuckAfter = properties.saga().stuckAfter();
        Gauge.builder("saga.stuck", compensationFailed, AtomicLong::get).tag("reason", "compensation_failed")
                .description("Sagas detenidas que requieren a un operador").register(meters);
        Gauge.builder("saga.stuck", noProgress, AtomicLong::get).tag("reason", "no_progress")
                .description("Sagas abiertas sin avance durante saga.stuck-after").register(meters);
        Gauge.builder("outbox.pending", outboxPending, AtomicLong::get)
                .description("Eventos del outbox sin publicar").register(meters);
        Gauge.builder("outbox.oldest.age", this, SagaGauges::oldestAgeSeconds).baseUnit("seconds")
                .description("Edad del evento más viejo sin publicar").register(meters);
    }

    @Scheduled(fixedDelayString = "${verabank.saga.stuck-refresh:15s}")
    public void scheduledRefresh() {
        try {
            refresh();
        } catch (RuntimeException e) {
            log.warn("no se pudieron actualizar los gauges de la saga: {}", e.getMessage());
        }
    }

    /** Una consulta por indicador; los gauges leen los valores guardados. */
    public void refresh() {
        compensationFailed.set(sagas.countCompensationFailed());
        noProgress.set(sagas.countNoProgress(stuckAfter));
        PendingOutbox pending = jdbc.sql(
                "SELECT count(*) AS pending, min(created_at) AS oldest FROM core.outbox WHERE published_at IS NULL")
                .query((rs, i) -> new PendingOutbox(rs.getLong("pending"), Sql.instant(rs, "oldest")))
                .single();
        outboxPending.set(pending.count());
        outboxOldest.set(pending.oldest());
        if (compensationFailed.get() > 0) {
            List<UUID> ids = sagas.compensationFailedIds();
            log.error("saga.stuck[compensation_failed] = {}: fondos retenidos, requiere un operador "
                    + "(POST /internal/v1/sagas/{id}/resume). Sagas: {}", compensationFailed.get(), ids);
        }
        if (noProgress.get() > 0) {
            log.error("saga.stuck[no_progress] = {}: sin avance desde hace más de {}. Sagas: {}",
                    noProgress.get(), stuckAfter, sagas.noProgress(stuckAfter));
        }
    }

    private record PendingOutbox(long count, Instant oldest) {
    }

    private double oldestAgeSeconds() {
        Instant oldest = outboxOldest.get();
        return oldest == null ? 0 : Math.max(0, Duration.between(oldest, Instant.now()).toMillis() / 1000.0);
    }
}
