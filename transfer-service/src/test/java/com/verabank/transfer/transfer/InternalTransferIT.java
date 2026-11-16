package com.verabank.transfer.transfer;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.verabank.transfer.support.Bank;
import com.verabank.transfer.support.Http;
import com.verabank.transfer.support.IntegrationTest;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.transaction.PlatformTransactionManager;

/** Transferencia interna completa: asientos, saga, outbox y auditoría en las mismas transacciones. */
class InternalTransferIT extends IntegrationTest {

    @Autowired
    PlatformTransactionManager txManager;

    @Autowired
    ObjectMapper mapper;

    private Http http;
    private Bank bank;

    @BeforeEach
    void setUp() {
        http = new Http(port);
        bank = new Bank(jdbc, txManager);
    }

    @Test
    void completaConAsientosDcYDevuelveElContrato() {
        UUID source = bank.account("500.00");
        UUID dest = bank.account("20.00");

        Http.Response r = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(source, dest, "120.50"));

        assertThat(r.status()).isEqualTo(201);
        JsonNode body = r.json();
        assertThat(body.path("status").asText()).isEqualTo("COMPLETED");
        assertThat(body.path("amount").isTextual()).isTrue();
        assertThat(body.path("amount").asText()).isEqualTo("120.50");
        assertThat(body.path("source_account_id").asText()).isEqualTo(source.toString());
        assertThat(body.path("dest_account_id").asText()).isEqualTo(dest.toString());
        assertThat(body.path("fraud_decision").asText()).isEqualTo("APPROVE");
        assertThat(body.path("completed_at").asText()).isNotBlank();
        assertThat(body.path("occurred_at").asText()).isEqualTo("2026-03-01T10:15:30Z");
        assertThat(body.has("failure_reason")).isTrue();

        UUID transferId = UUID.fromString(body.path("transfer_id").asText());
        assertThat(bank.balance(source)).isEqualByComparingTo("379.50");
        assertThat(bank.balance(dest)).isEqualByComparingTo("140.50");
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        List<String> entries = jdbc.sql("""
                SELECT account_id || ':' || direction || ':' || entry_kind || ':' || amount || ':' || balance_after
                FROM core.transactions WHERE transfer_id = :t ORDER BY entry_id""")
                .param("t", transferId).query(String.class).list();
        assertThat(entries).containsExactly(
                source + ":D:TRANSFER:120.50:379.50",
                dest + ":C:TRANSFER:120.50:140.50");
        assertThat(jdbc.sql("SELECT status || ':' || current_step FROM core.saga_state WHERE saga_id = :t")
                .param("t", transferId).query(String.class).single()).isEqualTo("COMPLETED:CREDITED");
        assertThat(jdbc.sql("""
                SELECT fraud_decision || ':' || decision_source || ':' || model_version
                FROM core.transfers WHERE transfer_id = :t""")
                .param("t", transferId).query(String.class).single()).isEqualTo("APPROVE:MODEL:stub-0");
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }

    @Test
    void elOutboxRecibeInitiatedYCompletedConClaveDeOrigenYCorrelationId() throws Exception {
        UUID source = bank.account("500.00");
        UUID dest = bank.account("0");
        UUID cid = UUID.randomUUID();

        Http.Response r = http.send("POST", "/api/v1/transfers", Map.of("X-API-Key", KEY_WEB,
                "Idempotency-Key", UUID.randomUUID().toString(), "X-Correlation-Id", cid.toString()),
                Bank.internal(source, dest, "10.00"));
        assertThat(r.status()).isEqualTo(201);
        UUID transferId = UUID.fromString(r.json().path("transfer_id").asText());

        List<Map<String, Object>> rows = jdbc.sql("""
                SELECT event_type, topic, message_key, aggregate_type, payload::text AS envelope
                FROM core.outbox WHERE aggregate_id = :t ORDER BY outbox_id""")
                .param("t", transferId).query().listOfRows();
        assertThat(rows).extracting(m -> m.get("event_type")).containsExactly("TransferInitiated", "TransferScored", "TransferCompleted");
        for (Map<String, Object> row : rows) {
            assertThat(row.get("topic")).isEqualTo("transfer.events.v1");
            assertThat(row.get("message_key")).isEqualTo(source.toString());
            assertThat(row.get("aggregate_type")).isEqualTo("transfer");
            JsonNode envelope = mapper.readTree((String) row.get("envelope"));
            assertThat(envelope.path("correlation_id").asText()).isEqualTo(cid.toString());
            assertThat(envelope.path("aggregate_id").asText()).isEqualTo(transferId.toString());
            assertThat(envelope.path("payload").path("transfer_id").asText()).isEqualTo(transferId.toString());
            if (!"TransferScored".equals(row.get("event_type"))) {   // §3.3: Scored no lleva el monto
                assertThat(envelope.path("payload").path("amount").asText()).isEqualTo("10.00");
            }
        }
    }

    @Test
    void cadaCambioDeEstadoSeAuditaEnLaCadenaConSuCorrelationId() {
        UUID source = bank.account("500.00");
        UUID dest = bank.account("0");
        UUID cid = UUID.randomUUID();

        Http.Response r = http.send("POST", "/api/v1/transfers", Map.of("X-API-Key", KEY_MOBILE,
                "Idempotency-Key", UUID.randomUUID().toString(), "X-Correlation-Id", cid.toString()),
                Bank.internal(source, dest, "10.00"));
        UUID transferId = UUID.fromString(r.json().path("transfer_id").asText());

        List<Map<String, Object>> audit = jdbc.sql("""
                SELECT audit_id, actor, action, correlation_id, detail->>'from_status' AS from_status,
                       detail->>'to_status' AS to_status, detail->>'client_id' AS client_id
                FROM audit.audit_log WHERE entity_type = 'TRANSFER' AND entity_id = :t ORDER BY audit_id""")
                .param("t", transferId.toString()).query().listOfRows();
        assertThat(audit).extracting(m -> m.get("action")).containsExactly("TRANSFER_CREATED", "TRANSFER_COMPLETED");
        assertThat(audit).extracting(m -> m.get("correlation_id")).containsOnly(cid);
        assertThat(audit).extracting(m -> m.get("to_status")).containsExactly("PENDING", "COMPLETED");
        assertThat(audit.get(0).get("actor")).isEqualTo("channel:" + CLIENT_MOBILE);
        assertThat(audit.get(1).get("from_status")).isEqualTo("PENDING");
        assertThat(audit).extracting(m -> m.get("client_id")).containsOnly(CLIENT_MOBILE);

        // La cadena la mantiene el trigger: cada fila enlaza con el hash de la anterior.
        long brokenLinks = jdbc.sql("""
                SELECT count(*) FROM audit.audit_log a JOIN audit.audit_log p ON p.audit_id = a.audit_id - 1
                WHERE a.prev_hash IS DISTINCT FROM p.row_hash""").query(Long.class).single();
        assertThat(brokenLinks).isZero();
    }

    /**
     * Atomicidad: el cambio de estado y su fila de auditoría se confirman en la misma
     * transacción, es decir, con el mismo xmin. Una auditoría en otra transacción (p. ej.
     * REQUIRES_NEW) tendría otro xmin aunque el pool no se agote.
     */
    @Test
    void laTransferenciaCompletadaYSuAuditoriaComparteXmin() {
        UUID source = bank.account("500.00");
        UUID dest = bank.account("0");
        Http.Response r = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(source, dest, "10.00"));
        UUID transferId = UUID.fromString(r.json().path("transfer_id").asText());

        String transferXmin = jdbc.sql("SELECT xmin::text FROM core.transfers WHERE transfer_id = :t")
                .param("t", transferId).query(String.class).single();
        String auditXmin = jdbc.sql("""
                SELECT xmin::text FROM audit.audit_log
                WHERE entity_type = 'TRANSFER' AND entity_id = :t AND action = 'TRANSFER_COMPLETED'""")
                .param("t", transferId.toString()).query(String.class).single();

        assertThat(auditXmin).as("xmin de TRANSFER_COMPLETED frente al de core.transfers").isEqualTo(transferXmin);
    }

    @ParameterizedTest
    @ValueSource(strings = {
        // monto como número JSON, no como cadena decimal
        """
        {"source_account_id":"%1$s","dest_account_id":"%2$s","dest_country":"PE","rail":"INTERNAL",
         "transfer_type":"TRANSFER","amount":10.00,"currency":"PEN","occurred_at":"2026-03-01T10:15:30Z"}""",
        // monto cero
        """
        {"source_account_id":"%1$s","dest_account_id":"%2$s","dest_country":"PE","rail":"INTERNAL",
         "transfer_type":"TRANSFER","amount":"0.00","currency":"PEN","occurred_at":"2026-03-01T10:15:30Z"}""",
        // tres decimales
        """
        {"source_account_id":"%1$s","dest_account_id":"%2$s","dest_country":"PE","rail":"INTERNAL",
         "transfer_type":"TRANSFER","amount":"1.005","currency":"PEN","occurred_at":"2026-03-01T10:15:30Z"}""",
        // INTERNAL sin dest_account_id
        """
        {"source_account_id":"%1$s","dest_country":"PE","rail":"INTERNAL",
         "transfer_type":"TRANSFER","amount":"1.00","currency":"PEN","occurred_at":"2026-03-01T10:15:30Z"}""",
        // origen igual a destino
        """
        {"source_account_id":"%1$s","dest_account_id":"%1$s","dest_country":"PE","rail":"INTERNAL",
         "transfer_type":"TRANSFER","amount":"1.00","currency":"PEN","occurred_at":"2026-03-01T10:15:30Z"}""",
        // moneda distinta de la de las cuentas
        """
        {"source_account_id":"%1$s","dest_account_id":"%2$s","dest_country":"PE","rail":"INTERNAL",
         "transfer_type":"TRANSFER","amount":"1.00","currency":"USD","occurred_at":"2026-03-01T10:15:30Z"}""",
        // enum desconocido
        """
        {"source_account_id":"%1$s","dest_account_id":"%2$s","dest_country":"PE","rail":"SWIFT",
         "transfer_type":"TRANSFER","amount":"1.00","currency":"PEN","occurred_at":"2026-03-01T10:15:30Z"}""",
        // fecha sin zona
        """
        {"source_account_id":"%1$s","dest_account_id":"%2$s","dest_country":"PE","rail":"INTERNAL",
         "transfer_type":"TRANSFER","amount":"1.00","currency":"PEN","occurred_at":"2026-03-01T10:15:30"}""",
        // país en minúsculas
        """
        {"source_account_id":"%1$s","dest_account_id":"%2$s","dest_country":"pe","rail":"INTERNAL",
         "transfer_type":"TRANSFER","amount":"1.00","currency":"PEN","occurred_at":"2026-03-01T10:15:30Z"}""",
        "no es json"
    })
    void cuerpoInvalidoEs400ValidationErrorSinEfectos(String template) {
        UUID source = bank.account("100.00");
        UUID dest = bank.account("0");
        UUID key = UUID.randomUUID();

        Http.Response r = http.postTransfer(KEY_WEB, key, template.formatted(source, dest));

        assertThat(r.status()).isEqualTo(400);
        assertThat(r.header("Content-Type")).startsWith("application/problem+json");
        assertThat(r.json().path("type").asText()).isEqualTo("validation-error");
        assertThat(r.json().path("correlation_id").asText()).isNotBlank();
        assertThat(bank.countTransfers(CLIENT_WEB, key)).isZero();
        assertThat(bank.balance(source)).isEqualByComparingTo("100.00");
    }

    @Test
    void externaAprobadaQuedaEnProcessingConSuRetencionHastaLaFase2() {
        UUID source = bank.account("100.00");
        String body = """
                {"source_account_id":"%s","dest_external_ref":"CCI-00211234","dest_country":"PE","rail":"EXTERNAL",
                 "transfer_type":"TRANSFER","amount":"30.00","currency":"PEN","occurred_at":"2026-03-01T10:15:30Z"}"""
                .formatted(source);

        Http.Response r = http.postTransfer(KEY_WEB, UUID.randomUUID(), body);

        assertThat(r.status()).isEqualTo(201);
        assertThat(r.json().path("status").asText()).isEqualTo("PROCESSING");
        assertThat(r.json().path("dest_account_id").isNull()).isTrue();
        assertThat(bank.held(source)).isEqualByComparingTo("30.00");
        assertThat(bank.balance(source)).isEqualByComparingTo("100.00");
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }
}
