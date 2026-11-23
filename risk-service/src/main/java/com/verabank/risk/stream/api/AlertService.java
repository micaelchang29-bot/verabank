package com.verabank.risk.stream.api;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.verabank.risk.shared.ApiException;
import io.quarkus.reactive.datasource.ReactiveDataSource;
import io.smallrye.mutiny.Uni;
import io.vertx.core.json.JsonObject;
import io.vertx.mutiny.pgclient.PgPool;
import io.vertx.mutiny.sqlclient.Row;
import io.vertx.mutiny.sqlclient.Tuple;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Base64;
import java.util.List;
import java.util.UUID;

/** Cola de alertas AML y su revisión (D4): lectura con cursor por clave y transiciones auditadas con audit.append. */
@ApplicationScoped
public class AlertService {

    static final int DEFAULT_LIMIT = 50;
    static final int MAX_LIMIT = 200;
    static final String AUDIT_ACTION = "AML_ALERT_STATUS_CHANGED";
    /** Límite de la transacción que audita: la espera de la cadena puede pasar de los 250 ms del rol (ver transition). */
    static final String AUDIT_STATEMENT_TIMEOUT = "5s";
    static final String AUDIT_ENTITY = "AML_ALERT";

    private static final String RANK = "CASE severity WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END";
    private static final String COLUMNS = """
            alert_id, rule_code, rule_version, rule_params::text AS rule_params, severity, status,
            subject_account_id, transfer_ids, total_amount, window_start, window_end, created_at, closed_at, closed_by
            """;

    @Inject
    @ReactiveDataSource("stream")
    PgPool pool;
    @Inject
    ObjectMapper mapper;

    /** Cola de revisión: de mayor a menor severidad y, a igual severidad, de la más antigua a la más reciente. */
    public Uni<AlertPage> list(String status, String severity, String ruleCode, int limit, String cursor) {
        List<String> where = new ArrayList<>();
        Tuple args = Tuple.tuple();
        if (status != null) {
            args.addValue(status);
            where.add("status = $" + args.size());
        }
        if (severity != null) {
            args.addValue(severity);
            where.add("severity = $" + args.size());
        }
        if (ruleCode != null) {
            args.addValue(ruleCode);
            where.add("rule_code = $" + args.size());
        }
        if (cursor != null) {
            Cursor c = Cursor.decode(cursor);
            args.addValue(c.rank).addValue(c.createdAt.atOffset(ZoneOffset.UTC)).addValue(c.alertId);
            int r = args.size() - 2;
            where.add("(" + RANK + " < $" + r + " OR (" + RANK + " = $" + r + " AND (created_at, alert_id) > ($"
                    + (r + 1) + ", $" + (r + 2) + ")))");
        }
        String sql = "SELECT " + COLUMNS + ", " + RANK + " AS rank FROM risk.aml_alerts"
                + (where.isEmpty() ? "" : " WHERE " + String.join(" AND ", where))
                + " ORDER BY " + RANK + " DESC, created_at, alert_id LIMIT " + (limit + 1);
        return pool.preparedQuery(sql).execute(args).map(rows -> {
            List<AlertView> items = new ArrayList<>();
            Row last = null;
            for (Row row : rows) {
                if (items.size() == limit) {
                    break;
                }
                items.add(view(row));
                last = row;
            }
            String next = rows.size() > limit && last != null
                    ? new Cursor(last.getInteger("rank"), last.getOffsetDateTime("created_at").toInstant(),
                            last.getUUID("alert_id")).encode()
                    : null;
            return new AlertPage(items, next);
        });
    }

    /** Avanza o cierra una alerta; todo (bloqueo, cambio y auditoría) en una transacción. */
    public Uni<AlertView> transition(UUID alertId, String newStatus, String comment, String channel,
            UUID correlationId) {
        return pool.withTransaction(conn -> conn
                .preparedQuery("SELECT " + COLUMNS + " FROM risk.aml_alerts WHERE alert_id = $1 FOR UPDATE")
                .execute(Tuple.of(alertId)).flatMap(rows -> {
                    if (rows.size() == 0) {
                        return Uni.createFrom().failure(new ApiException(404, "alert-not-found",
                                "Alerta no encontrada", "La alerta " + alertId + " no existe"));
                    }
                    Row current = rows.iterator().next();
                    String from = current.getString("status");
                    if (!AlertTransitions.allowed(from, newStatus)) {
                        return Uni.createFrom().failure(new ApiException(409, "alert-transition-invalid",
                                "Transición de alerta no válida",
                                "La alerta está en " + from + " y no puede pasar a " + newStatus));
                    }
                    boolean closes = AlertTransitions.closes(newStatus);
                    OffsetDateTime closedAt = closes ? OffsetDateTime.now(ZoneOffset.UTC) : null;
                    return conn.preparedQuery("""
                            UPDATE risk.aml_alerts SET status = $2, closed_at = $3, closed_by = $4
                             WHERE alert_id = $1 RETURNING\s""" + COLUMNS)
                            .execute(Tuple.of(alertId, newStatus, closedAt, closes ? channel : null))
                            .flatMap(updated -> {
                                // La cadena de hash de auditoría se serializa hasta el commit: se escribe al final.
                                // Su espera cuenta en el statement_timeout de 250 ms de risk_svc (respaldo del scoring);
                                // con transfer-service reteniendo la cadena, la transición fallaba. Solo en esta
                                // transacción (SET LOCAL) el límite sube a AUDIT_STATEMENT_TIMEOUT.
                                return conn.query("SET LOCAL statement_timeout = '" + AUDIT_STATEMENT_TIMEOUT + "'")
                                        .execute()
                                        .flatMap(set -> conn.preparedQuery("SELECT audit.append($1, $2, $3, $4, $5, $6)")
                                        .execute(Tuple.tuple().addValue(channel).addValue(AUDIT_ACTION)
                                                .addValue(AUDIT_ENTITY).addValue(alertId.toString())
                                                .addValue(correlationId)
                                                .addValue(new JsonObject(detail(current, from, newStatus, comment))))
                                        .replaceWith(view(updated.iterator().next())));
                            });
                }));
    }

    private String detail(Row current, String from, String to, String comment) {
        ObjectNode detail = mapper.createObjectNode();
        detail.put("from", from);
        detail.put("to", to);
        detail.put("rule_code", current.getString("rule_code"));
        detail.put("subject_account_id", current.getUUID("subject_account_id").toString());
        if (comment != null) {
            detail.put("comment", comment);
        }
        return detail.toString();
    }

    private AlertView view(Row row) {
        JsonNode params;
        try {
            params = mapper.readTree(row.getString("rule_params"));
        } catch (JsonProcessingException e) {
            throw new IllegalStateException("rule_params ilegible", e);
        }
        return new AlertView(row.getUUID("alert_id").toString(), row.getString("rule_code"),
                row.getInteger("rule_version"), params, row.getString("severity"), row.getString("status"),
                row.getUUID("subject_account_id").toString(),
                Arrays.stream(row.getArrayOfUUIDs("transfer_ids")).map(UUID::toString).toList(),
                row.getNumeric("total_amount").bigDecimalValue().toPlainString(),
                row.getOffsetDateTime("window_start").toInstant().toString(),
                row.getOffsetDateTime("window_end").toInstant().toString(),
                row.getOffsetDateTime("created_at").toInstant().toString(),
                row.getOffsetDateTime("closed_at") == null ? null : row.getOffsetDateTime("closed_at").toInstant().toString(),
                row.getString("closed_by"));
    }

    /** Posición en el orden de la cola: opaco para el cliente. */
    record Cursor(int rank, Instant createdAt, UUID alertId) {

        String encode() {
            return Base64.getUrlEncoder().withoutPadding()
                    .encodeToString((rank + "|" + createdAt + "|" + alertId).getBytes(StandardCharsets.UTF_8));
        }

        static Cursor decode(String value) {
            try {
                String[] parts = new String(Base64.getUrlDecoder().decode(value), StandardCharsets.UTF_8).split("\\|");
                if (parts.length != 3) {
                    throw new IllegalArgumentException();
                }
                int rank = Integer.parseInt(parts[0]);
                if (rank < 1 || rank > 3) {
                    throw new IllegalArgumentException();
                }
                return new Cursor(rank, Instant.parse(parts[1]), UUID.fromString(parts[2]));
            } catch (RuntimeException e) {
                throw ApiException.validation("cursor inválido");
            }
        }
    }
}
