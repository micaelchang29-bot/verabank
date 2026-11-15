package com.verabank.transfer.transfer;

import java.time.Duration;
import java.util.UUID;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

/** core.saga_state. En la fase 1 solo registra el paso; el poller de la fase 2 usará wake_at. */
@Repository
public class SagaRepository {

    private final JdbcClient jdbc;

    public SagaRepository(JdbcClient jdbc) {
        this.jdbc = jdbc;
    }

    public void start(UUID sagaId, String step, Duration wakeAfter) {
        jdbc.sql("""
                INSERT INTO core.saga_state (saga_id, current_step, status, wake_at)
                VALUES (:id, :step, 'RUNNING', now() + make_interval(secs => :secs))""")
                .param("id", sagaId)
                .param("step", step)
                .param("secs", wakeAfter.toMillis() / 1000.0)
                .update();
    }

    public void advance(UUID sagaId, String step, String status) {
        jdbc.sql("""
                UPDATE core.saga_state SET current_step = :step, status = :status, wake_at = NULL,
                    last_error = NULL, version = version + 1, updated_at = now()
                WHERE saga_id = :id""")
                .param("id", sagaId)
                .param("step", step)
                .param("status", status)
                .update();
    }

    /**
     * Reclama una saga detenida (wake_at vencido) para reanudarla. Solo un reclamante gana:
     * el resto ve wake_at ya movido al futuro.
     */
    public boolean claimStalled(UUID sagaId, Duration nextWake) {
        return jdbc.sql("""
                UPDATE core.saga_state SET wake_at = now() + make_interval(secs => :secs),
                    attempt = attempt + 1, version = version + 1, updated_at = now()
                WHERE saga_id = :id AND status = 'RUNNING' AND wake_at <= now()""")
                .param("id", sagaId)
                .param("secs", nextWake.toMillis() / 1000.0)
                .update() == 1;
    }

    public void recordError(UUID sagaId, String error) {
        jdbc.sql("UPDATE core.saga_state SET last_error = :error, updated_at = now() WHERE saga_id = :id")
                .param("id", sagaId)
                .param("error", error.length() > 500 ? error.substring(0, 500) : error)
                .update();
    }
}
