package com.verabank.transfer.saga;

import static org.assertj.core.api.Assertions.assertThat;

import com.verabank.transfer.scoring.FraudScorer;
import com.verabank.transfer.support.Bank;
import com.verabank.transfer.support.Events;
import com.verabank.transfer.support.Http;
import com.verabank.transfer.support.IntegrationTest;
import com.verabank.transfer.support.Scoring;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;
import org.springframework.transaction.PlatformTransactionManager;

/**
 * tx2 con rail EXTERNAL (S5) y las ramas BLOCK (S2) y REVIEW (S3) de docs/saga.md §2: estados,
 * eventos de outbox y auditoría en la misma transacción, y todos los eventos contra su esquema.
 */
@TestPropertySource(properties = "verabank.saga.rail-reply-timeout=45s")
class ExternalTransferIT extends IntegrationTest {

    @MockitoSpyBean
    FraudScorer scorer;

    @Autowired
    PlatformTransactionManager txManager;

    private Http http;
    private Bank bank;
    private UUID source;

    @BeforeEach
    void setUp() {
        http = new Http(port);
        bank = new Bank(jdbc, txManager);
        source = bank.account("500.00");
        Scoring.blockAndReview(scorer);
    }

    static String external(UUID source, String amount) {
        return """
                {"source_account_id":"%s","dest_external_ref":"PE-BCP-0012345678","dest_country":"PE",
                 "rail":"EXTERNAL","transfer_type":"TRANSFER","amount":"%s","currency":"PEN",
                 "occurred_at":"2026-03-01T10:15:30Z"}""".formatted(source, amount);
    }

    @Test
    void unaTransferenciaExternaAprobadaQuedaEsperandoAlRailConSuInstruccionEnElOutbox() {
        Http.Response r = http.postTransfer(KEY_WEB, UUID.randomUUID(), external(source, "120.00"));

        assertThat(r.status()).isEqualTo(201);
        assertThat(r.json().path("status").asText()).isEqualTo("PROCESSING");
        UUID id = UUID.fromString(r.json().path("transfer_id").asText());

        assertThat(jdbc.sql("""
                SELECT status || ':' || current_step || ':' || (wake_at IS NOT NULL) || ':' || attempt
                FROM core.saga_state WHERE saga_id = :t""").param("t", id).query(String.class).single())
                .isEqualTo("WAITING_REPLY:CREDIT_REQUESTED:true:0");
        // wake_at = now + saga.rail-reply-timeout (45 s en esta clase).
        Double secs = jdbc.sql("SELECT EXTRACT(EPOCH FROM (wake_at - now())) FROM core.saga_state WHERE saga_id = :t")
                .param("t", id).query(Double.class).single();
        assertThat(secs).isBetween(35.0, 45.5);
        // La retención sigue: nada se asienta hasta la respuesta del rail.
        assertThat(bank.held(source)).isEqualByComparingTo("120.00");
        assertThat(bank.balance(source)).isEqualByComparingTo("500.00");
        assertThat(bank.countEntries(id, "D")).isZero();
        assertThat(bank.reconcile()).containsExactly(0, 0);

        List<Events.Row> events = Events.outbox(jdbc, id);
        assertThat(events).extracting(Events.Row::eventType)
                .containsExactly("TransferInitiated", "TransferScored", "CreditInstruction");
        Events.Row instruction = events.get(2);
        assertThat(instruction.topic()).isEqualTo("rail.commands.v1");
        assertThat(instruction.key()).isEqualTo(id.toString());
        assertThat(instruction.payload().path("dest_external_ref").asText()).isEqualTo("PE-BCP-0012345678");
        assertThat(instruction.payload().path("amount").asText()).isEqualTo("120.00");
        assertThat(events.get(1).topic()).isEqualTo("transfer.events.v1");
        Events.assertAllValid(jdbc, id);

        // La auditoría del paso va en la misma transacción que el cambio.
        assertThat(jdbc.sql("""
                SELECT action || ':' || (detail->>'to_status') FROM audit.audit_log
                WHERE entity_id = :t ORDER BY audit_id""").param("t", id.toString()).query(String.class).list())
                .containsExactly("TRANSFER_CREATED:PENDING", "TRANSFER_PROCESSING:PROCESSING");
    }

    @Test
    void laRamaBlockLiberaLaRetencionYEmiteScoredYRejected() {
        Http.Response r = http.postTransfer(KEY_WEB, UUID.randomUUID(), external(source, "66.66"));

        assertThat(r.status()).isEqualTo(422);
        UUID id = jdbc.sql("SELECT transfer_id FROM core.transfers WHERE source_account_id = :s")
                .param("s", source).query(UUID.class).single();
        assertThat(jdbc.sql("SELECT status || ':' || current_step FROM core.saga_state WHERE saga_id = :t")
                .param("t", id).query(String.class).single()).isEqualTo("COMPENSATED:DONE");
        assertThat(jdbc.sql("SELECT status || ':' || failure_reason FROM core.transfers WHERE transfer_id = :t")
                .param("t", id).query(String.class).single()).isEqualTo("REJECTED:RISK_BLOCK");
        assertThat(Events.types(jdbc, id)).containsExactly("TransferInitiated", "TransferScored", "TransferRejected");
        assertThat(Events.outbox(jdbc, id).get(2).payload().path("reason").asText()).isEqualTo("RISK_BLOCK");
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        assertThat(bank.reconcile()).containsExactly(0, 0);
        Events.assertAllValid(jdbc, id);
    }

    @Test
    void laRamaReviewRetieneSinWakeAtYEmiteScoredYHeld() {
        Http.Response r = http.postTransfer(KEY_WEB, UUID.randomUUID(), external(source, "77.77"));

        assertThat(r.status()).isEqualTo(201);
        assertThat(r.json().path("status").asText()).isEqualTo("IN_REVIEW");
        UUID id = UUID.fromString(r.json().path("transfer_id").asText());
        assertThat(jdbc.sql("SELECT status || ':' || current_step || ':' || coalesce(wake_at::text, 'null') "
                + "FROM core.saga_state WHERE saga_id = :t").param("t", id).query(String.class).single())
                .isEqualTo("IN_REVIEW:IN_REVIEW:null");
        assertThat(Events.types(jdbc, id)).containsExactly("TransferInitiated", "TransferScored", "TransferHeld");
        assertThat(bank.held(source)).isEqualByComparingTo("77.77");
        assertThat(bank.reconcile()).containsExactly(0, 0);
        Events.assertAllValid(jdbc, id);
    }
}
