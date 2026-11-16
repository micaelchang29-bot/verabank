package com.verabank.transfer.saga;

import static com.verabank.transfer.config.Sql.instant;
import static com.verabank.transfer.config.Sql.uuid;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

/**
 * core.saga_state. Toda transición se condiciona a {@code version} (bloqueo optimista, docs/saga.md §1.5):
 * el que llega tarde no actualiza ninguna fila y lo trata como conflicto.
 */
@Repository
public class SagaRepository {

    private static final String COLUMNS =
            "saga_id, current_step, status, attempt, wake_at, last_error, payload::text AS payload, version, "
                    + "created_at, updated_at";

    private final JdbcClient jdbc;
    private final ObjectMapper mapper;

    public SagaRepository(JdbcClient jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    /** Resultado de registrar un fallo local (S14/S15). */
    public record Failure(int attempt, String status, String step) {

        public boolean stopped() {
            return SagaStates.COMPENSATION_FAILED.equals(status);
        }
    }

    public void start(UUID sagaId, String step, Duration wakeAfter) {
        jdbc.sql("""
                INSERT INTO core.saga_state (saga_id, current_step, status, wake_at)
                VALUES (:id, :step, 'RUNNING', now() + make_interval(secs => :secs))""")
                .param("id", sagaId)
                .param("step", step)
                .param("secs", seconds(wakeAfter))
                .update();
    }

    public Optional<SagaRow> find(UUID sagaId) {
        return jdbc.sql("SELECT " + COLUMNS + " FROM core.saga_state WHERE saga_id = :id")
                .param("id", sagaId)
                .query(this::map)
                .optional();
    }

    public Optional<SagaRow> findForUpdate(UUID sagaId) {
        return jdbc.sql("SELECT " + COLUMNS + " FROM core.saga_state WHERE saga_id = :id FOR UPDATE")
                .param("id", sagaId)
                .query(this::map)
                .optional();
    }

    /**
     * Transición condicionada a la versión leída. Reinicia {@code attempt} y {@code last_error}
     * (el paso terminó bien), sube la versión y actualiza {@code updated_at}. {@code wakeIn} nulo
     * deja {@code wake_at} en NULL; {@code payloadPatch} se mezcla con el payload existente.
     *
     * @return {@code false} si la versión ya no es la esperada (otro actor transitó la saga)
     */
    public boolean advance(UUID sagaId, long expectedVersion, String step, String status, Duration wakeIn,
            Map<String, ?> payloadPatch) {
        return jdbc.sql("""
                UPDATE core.saga_state SET current_step = :step, status = :status,
                    wake_at = CASE WHEN CAST(:secs AS double precision) IS NULL THEN NULL
                                   ELSE now() + make_interval(secs => CAST(:secs AS double precision)) END,
                    payload = payload || CAST(:patch AS jsonb),
                    attempt = 0, last_error = NULL, version = version + 1, updated_at = now()
                WHERE saga_id = :id AND version = :version""")
                .param("id", sagaId)
                .param("version", expectedVersion)
                .param("step", step)
                .param("status", status)
                .param("secs", wakeIn == null ? null : seconds(wakeIn))
                .param("patch", json(payloadPatch == null ? Map.of() : payloadPatch))
                .update() == 1;
    }

    /**
     * Compuerta de versión de una transacción que cierra un paso: sube la versión solo si sigue siendo
     * la leída y devuelve la nueva (que usará la transición final). Es la primera escritura de la
     * transacción, para tomar el bloqueo de la saga antes que el de la transferencia y las cuentas:
     * un orden único evita interbloqueos con el sondeo y el consumidor.
     */
    public Optional<Long> touch(UUID sagaId, long expectedVersion) {
        return jdbc.sql("""
                UPDATE core.saga_state SET version = version + 1, updated_at = now()
                WHERE saga_id = :id AND version = :version
                RETURNING version""")
                .param("id", sagaId)
                .param("version", expectedVersion)
                .query(Long.class)
                .optional();
    }

    /**
     * Reclama una saga RUNNING con {@code wake_at} vencido: empuja {@code wake_at} al fin del
     * arrendamiento y sube la versión. Solo un reclamante gana; el resto ve {@code wake_at} en el
     * futuro. Devuelve la versión reclamada, con la que debe cerrarse tx2. {@code attempt} no se toca:
     * cuenta fallos, no reclamaciones.
     */
    public Optional<Long> claim(UUID sagaId, Duration lease) {
        return jdbc.sql("""
                UPDATE core.saga_state SET wake_at = now() + make_interval(secs => :secs),
                    version = version + 1, updated_at = now()
                WHERE saga_id = :id AND status = 'RUNNING' AND wake_at <= now()
                RETURNING version""")
                .param("id", sagaId)
                .param("secs", seconds(lease))
                .query(Long.class)
                .optional();
    }

    /**
     * Fallo local de un paso (S14); al llegar a {@code maxAttempts}, S15. El backoff es
     * {@code min(base · 2^(attempt−1), max)} con {@code attempt} ya incrementado. Se ejecuta en una
     * transacción propia, después de revertir la del paso.
     */
    public Optional<Failure> recordFailure(UUID sagaId, String error, Duration base, Duration max,
            int maxAttempts) {
        return jdbc.sql("""
                UPDATE core.saga_state SET attempt = attempt + 1, last_error = :error,
                    payload = CASE WHEN attempt + 1 >= :maxAttempts
                                   THEN payload || jsonb_build_object('resume_status', status) ELSE payload END,
                    wake_at = CASE WHEN attempt + 1 >= :maxAttempts THEN NULL
                                   ELSE now() + make_interval(secs => LEAST(:base * power(2, attempt), :max)) END,
                    status = CASE WHEN attempt + 1 >= :maxAttempts THEN 'COMPENSATION_FAILED' ELSE status END,
                    version = version + 1, updated_at = now()
                WHERE saga_id = :id AND status IN ('RUNNING', 'WAITING_REPLY', 'COMPENSATING')
                RETURNING attempt, status, current_step""")
                .param("id", sagaId)
                .param("error", truncate(error))
                .param("base", seconds(base))
                .param("max", seconds(max))
                .param("maxAttempts", maxAttempts)
                .query((rs, i) -> new Failure(rs.getInt("attempt"), rs.getString("status"),
                        rs.getString("current_step")))
                .optional();
    }

    /** S16: vuelve al estado en que se detuvo la saga. */
    public boolean resume(UUID sagaId, long expectedVersion, String resumeStatus) {
        return jdbc.sql("""
                UPDATE core.saga_state SET status = :status, attempt = 0, wake_at = now(), last_error = NULL,
                    version = version + 1, updated_at = now()
                WHERE saga_id = :id AND version = :version AND status = 'COMPENSATION_FAILED'""")
                .param("id", sagaId)
                .param("version", expectedVersion)
                .param("status", resumeStatus)
                .update() == 1;
    }

    /**
     * La saga vencida más antigua que no esté bloqueada por otra transacción (una por llamada: la
     * transacción del sondeo es una por saga).
     */
    public Optional<SagaRow> nextDue(List<String> statuses) {
        return jdbc.sql("SELECT " + COLUMNS + """
                 FROM core.saga_state
                WHERE wake_at <= now() AND status IN (:statuses)
                ORDER BY wake_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED""")
                .param("statuses", statuses)
                .query(this::map)
                .optional();
    }

    /** Segundos (redondeados hacia arriba) hasta el próximo wake_at de la saga, si lo tiene. */
    public Optional<Long> secondsUntilWake(UUID sagaId) {
        return jdbc.sql("""
                SELECT CEIL(EXTRACT(EPOCH FROM (wake_at - now())))::bigint
                FROM core.saga_state WHERE saga_id = :id AND wake_at IS NOT NULL""")
                .param("id", sagaId)
                .query(Long.class)
                .optional();
    }

    public long countCompensationFailed() {
        return jdbc.sql("SELECT count(*) FROM core.saga_state WHERE status = 'COMPENSATION_FAILED'")
                .query(Long.class).single();
    }

    /** Sagas ni finales ni en revisión con {@code updated_at} más viejo que el umbral (máx. 50 ids). */
    public List<UUID> noProgress(Duration olderThan) {
        return jdbc.sql("""
                SELECT saga_id FROM core.saga_state
                WHERE status IN ('RUNNING', 'WAITING_REPLY', 'COMPENSATING')
                  AND updated_at < now() - make_interval(secs => :secs)
                ORDER BY updated_at LIMIT 50""")
                .param("secs", seconds(olderThan))
                .query((rs, i) -> uuid(rs, "saga_id"))
                .list();
    }

    public long countNoProgress(Duration olderThan) {
        return jdbc.sql("""
                SELECT count(*) FROM core.saga_state
                WHERE status IN ('RUNNING', 'WAITING_REPLY', 'COMPENSATING')
                  AND updated_at < now() - make_interval(secs => :secs)""")
                .param("secs", seconds(olderThan))
                .query(Long.class).single();
    }

    public List<UUID> compensationFailedIds() {
        return jdbc.sql("SELECT saga_id FROM core.saga_state WHERE status = 'COMPENSATION_FAILED' "
                + "ORDER BY updated_at LIMIT 50")
                .query((rs, i) -> uuid(rs, "saga_id")).list();
    }

    private SagaRow map(ResultSet rs, int row) throws SQLException {
        try {
            return new SagaRow(uuid(rs, "saga_id"), rs.getString("current_step"), rs.getString("status"),
                    rs.getInt("attempt"), instant(rs, "wake_at"), rs.getString("last_error"),
                    mapper.readTree(rs.getString("payload")), rs.getLong("version"), instant(rs, "created_at"),
                    instant(rs, "updated_at"));
        } catch (JsonProcessingException e) {
            throw new SQLException("payload ilegible en saga_state", e);
        }
    }

    private String json(Map<String, ?> value) {
        try {
            return mapper.writeValueAsString(value);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }

    private static double seconds(Duration d) {
        return d.toMillis() / 1000.0;
    }

    private static String truncate(String error) {
        return error.length() > 500 ? error.substring(0, 500) : error;
    }
}
