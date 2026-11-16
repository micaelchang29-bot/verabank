package com.verabank.transfer.saga;

import static org.assertj.core.api.Assertions.assertThat;

import com.verabank.transfer.scoring.FraudScorer;
import com.verabank.transfer.support.Bank;
import com.verabank.transfer.support.Http;
import com.verabank.transfer.support.IntegrationTest;
import com.verabank.transfer.support.Scoring;
import io.micrometer.core.instrument.MeterRegistry;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.system.CapturedOutput;
import org.springframework.boot.test.system.OutputCaptureExtension;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;
import org.springframework.transaction.PlatformTransactionManager;

/**
 * Métricas de docs/saga.md §8 visibles en {@code /actuator/metrics/<nombre>}. Con {@code stuck-refresh}
 * de una hora, el único cálculo es el que la prueba dispara: leer la métrica no consulta la base.
 */
@ExtendWith(OutputCaptureExtension.class)
@TestPropertySource(properties = {"verabank.saga.stuck-refresh=1h", "verabank.saga.stuck-after=2s"})
class MetricsIT extends IntegrationTest {

    @Autowired
    SagaGauges gauges;

    @Autowired
    MeterRegistry meters;

    @Autowired
    PlatformTransactionManager txManager;

    @MockitoSpyBean
    FraudScorer scorer;

    private Http http;
    private Bank bank;
    private UUID source;

    @BeforeEach
    void setUp() {
        http = new Http(port);
        bank = new Bank(jdbc, txManager);
        source = bank.account("1000.00");
        Scoring.blockAndReview(scorer);
        gauges.refresh();
    }

    private double stuck(String reason) {
        return meters.get("saga.stuck").tag("reason", reason).gauge().value();
    }

    private double viaActuator(String name, String tag) {
        Http.Response r = http.get(null, "/actuator/metrics/" + name + (tag == null ? "" : "?tag=" + tag));
        assertThat(r.status()).as(name).isEqualTo(200);
        return r.json().path("measurements").get(0).path("value").asDouble();
    }

    private UUID externalTransfer(String amount) {
        Http.Response r = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.external(source, amount));
        return UUID.fromString(r.json().path("transfer_id").asText());
    }

    @Test
    void todasLasMetricasDeLaSagaSonVisiblesEnActuator() {
        for (String name : List.of("saga.stuck", "saga.compensation.duration", "saga.compensation_failed",
                "saga.conflicting_reply", "outbox.lag", "outbox.pending", "outbox.oldest.age", "outbox.dlt",
                "inbox.dlt", "inbox.duplicates")) {
            assertThat(http.get(null, "/actuator/metrics/" + name).status()).as(name).isEqualTo(200);
        }
        assertThat(http.get(null, "/actuator/metrics/saga.stuck?tag=reason:compensation_failed").status())
                .isEqualTo(200);
        assertThat(http.get(null, "/actuator/metrics/saga.stuck?tag=reason:no_progress").status()).isEqualTo(200);
    }

    @Test
    void sagaStuckSeCalculaConLaConsultaProgramadaYNoEnCadaLectura(CapturedOutput output) {
        double base = stuck("compensation_failed");
        UUID id = externalTransfer("30.00");

        jdbc.sql("""
                UPDATE core.saga_state SET status = 'COMPENSATION_FAILED', current_step = 'COMPENSATING',
                    wake_at = NULL, attempt = 5, last_error = 'fallo de prueba',
                    payload = payload || '{"resume_status":"COMPENSATING"}'::jsonb
                WHERE saga_id = :t""").param("t", id).update();

        // Leer la métrica, por el registro o por Actuator, no recalcula: sigue el valor de la última consulta.
        assertThat(stuck("compensation_failed")).isEqualTo(base);
        assertThat(viaActuator("saga.stuck", "reason:compensation_failed")).isEqualTo(base);

        gauges.refresh();   // la consulta programada

        assertThat(stuck("compensation_failed")).isEqualTo(base + 1);
        assertThat(viaActuator("saga.stuck", "reason:compensation_failed")).isEqualTo(base + 1);
        assertThat(output.getAll()).contains("ERROR").contains("saga.stuck[compensation_failed]")
                .contains(id.toString());
    }

    @Test
    void noProgressCuentaLasSagasAbiertasSinAvanceYNoLasQueEsperanAUnAnalista() {
        double base = stuck("no_progress");
        UUID waiting = externalTransfer("30.00");
        Http.Response review = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.external(source, "77.77"));
        UUID inReview = UUID.fromString(review.json().path("transfer_id").asText());
        assertThat(review.json().path("status").asText()).isEqualTo("IN_REVIEW");

        // La del rail lleva una hora sin avanzar (sin sondeo en esta clase); la del analista también.
        jdbc.sql("UPDATE core.saga_state SET updated_at = now() - interval '1 hour', wake_at = NULL "
                + "WHERE saga_id IN (:a, :b)").param("a", waiting).param("b", inReview).update();
        gauges.refresh();

        assertThat(stuck("no_progress")).as("solo cuenta la del rail").isEqualTo(base + 1);
    }

    @Test
    void outboxPendingYOldestAgeReflejanLosEventosSinPublicar() throws Exception {
        gauges.refresh();
        double pending = meters.get("outbox.pending").gauge().value();
        // El relay está apagado en esta clase: lo que se crea queda pendiente.
        externalTransfer("10.00");
        Thread.sleep(1200);

        gauges.refresh();

        assertThat(meters.get("outbox.pending").gauge().value()).isGreaterThanOrEqualTo(pending + 3);
        assertThat(meters.get("outbox.oldest.age").gauge().value()).isGreaterThan(0);
        assertThat(viaActuator("outbox.pending", null)).isGreaterThanOrEqualTo(pending + 3);
    }
}
