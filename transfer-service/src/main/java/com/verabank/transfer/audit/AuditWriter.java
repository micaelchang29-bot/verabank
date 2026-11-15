package com.verabank.transfer.audit;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.Map;
import java.util.UUID;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Component;

/**
 * Inserta en audit.audit_log dentro de la transacción del cambio auditado. audit_id,
 * prev_hash y row_hash los asigna el trigger audit.chain_hash (§4).
 *
 * <p>El trigger toma un bloqueo consultivo que serializa la cadena hasta el commit. Por eso
 * la auditoría debe ser lo último que hace cada transacción, después de bloquear cuentas y
 * transferencias: si no, una transacción con el bloqueo de la cadena podría esperar una fila
 * que tiene otra transacción que, a su vez, espera la cadena.
 */
@Component
public class AuditWriter {

    public static final String ENTITY_TRANSFER = "TRANSFER";

    private final JdbcClient jdbc;
    private final ObjectMapper mapper;

    public AuditWriter(JdbcClient jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    public void record(String actor, String action, String entityType, String entityId, UUID correlationId,
            Map<String, ?> detail) {
        jdbc.sql("""
                INSERT INTO audit.audit_log (actor, action, entity_type, entity_id, correlation_id, detail)
                VALUES (:actor, :action, :entityType, :entityId, :correlationId, CAST(:detail AS jsonb))""")
                .param("actor", actor)
                .param("action", action)
                .param("entityType", entityType)
                .param("entityId", entityId)
                .param("correlationId", correlationId)
                .param("detail", write(detail))
                .update();
    }

    private String write(Map<String, ?> detail) {
        try {
            return mapper.writeValueAsString(detail);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }
}
