package com.verabank.risk.stream;

import static com.verabank.risk.stream.StreamSupport.T0;
import static com.verabank.risk.stream.StreamSupport.alerts;
import static com.verabank.risk.stream.StreamSupport.published;
import static com.verabank.risk.stream.StreamSupport.send;
import static com.verabank.risk.stream.StreamSupport.sendAndWait;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.verabank.risk.stream.aml.AmlChain;
import com.verabank.risk.stream.aml.AmlRule;
import com.verabank.risk.stream.aml.RuleCatalog;
import com.verabank.risk.support.Db;
import io.quarkus.test.junit.QuarkusTest;
import io.quarkus.test.junit.TestProfile;
import jakarta.inject.Inject;
import java.time.Duration;
import java.time.Instant;
import java.util.UUID;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/** Cadena de reglas AML (§5a) con Kafka y PostgreSQL reales: T-15, bordes, versiones, exención y reentrega. */
@QuarkusTest
@TestProfile(StreamProfile.class)
class AmlRulesIT {

    private static final ObjectMapper JSON = new ObjectMapper();

    @Inject
    RuleCatalog catalog;
    @Inject
    AmlChain chain;

    @BeforeEach
    void reset() {
        StreamDb.reset();
        catalog.refresh().await().atMost(Duration.ofSeconds(10));
        FictitiousRule.enabled = false;
    }

    @AfterEach
    void restore() {
        FictitiousRule.enabled = false;
    }

    private static JsonNode json(String value) {
        try {
            return JSON.readTree(value);
        } catch (java.io.IOException e) {
            throw new IllegalStateException(e);
        }
    }

    private static Events.Transfer transfer(UUID source, String amount, Instant at) {
        return Events.transfer(source, amount, at);
    }

    // ---- T-15 ---------------------------------------------------------------------------------------------

    @Test
    void t15_threeTransfersOf9500RaiseOneHighAlertAndAFourthUpdatesTheSameOne() {
        UUID source = UUID.randomUUID();
        UUID t1 = UUID.randomUUID();
        UUID t2 = UUID.randomUUID();
        UUID t3 = UUID.randomUUID();
        sendAndWait(transfer(source, "9500.00", T0).transferId(t1));
        sendAndWait(transfer(source, "9500.00", T0.plus(Duration.ofHours(1))).transferId(t2));
        assertThat(alerts(source)).isZero();
        sendAndWait(transfer(source, "9500.00", T0.plus(Duration.ofHours(2))).transferId(t3));

        assertThat(alerts(source)).isEqualTo(1);
        assertThat(Db.queryString("SELECT severity FROM risk.aml_alerts WHERE subject_account_id = ?", source))
                .isEqualTo("HIGH");
        assertThat(Db.queryString("SELECT rule_code FROM risk.aml_alerts WHERE subject_account_id = ?", source))
                .isEqualTo("STRUCTURING");
        assertThat(Db.queryLong("SELECT cardinality(transfer_ids) FROM risk.aml_alerts WHERE subject_account_id = ?",
                source)).isEqualTo(3);
        assertThat(Db.queryDecimal("SELECT total_amount FROM risk.aml_alerts WHERE subject_account_id = ?", source))
                .isEqualByComparingTo("28500.00");
        UUID alertId = UUID.fromString(Db.queryString("SELECT alert_id::text FROM risk.aml_alerts WHERE subject_account_id = ?", source));
        String createdAt = Db.queryString("SELECT created_at::text FROM risk.aml_alerts WHERE alert_id = ?", alertId);
        // El analista ya la tomó: la ampliación no debe tocar status ni created_at.
        StreamDb.exec("UPDATE risk.aml_alerts SET status = 'IN_REVIEW' WHERE alert_id = ?", alertId);

        sendAndWait(transfer(source, "9500.00", T0.plus(Duration.ofHours(3))));

        assertThat(alerts(source)).isEqualTo(1);
        assertThat(Db.queryString("SELECT alert_id::text FROM risk.aml_alerts WHERE subject_account_id = ?", source))
                .isEqualTo(alertId.toString());
        assertThat(Db.queryLong("SELECT cardinality(transfer_ids) FROM risk.aml_alerts WHERE alert_id = ?", alertId))
                .isEqualTo(4);
        assertThat(Db.queryDecimal("SELECT total_amount FROM risk.aml_alerts WHERE alert_id = ?", alertId))
                .isEqualByComparingTo("38000.00");
        assertThat(Db.queryString("SELECT to_char(window_end AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS') "
                + "FROM risk.aml_alerts WHERE alert_id = ?", alertId)).isEqualTo("2026-03-01T13:00:00");   // la 4.ª
        assertThat(Db.queryString("SELECT status FROM risk.aml_alerts WHERE alert_id = ?", alertId)).isEqualTo("IN_REVIEW");
        assertThat(Db.queryString("SELECT created_at::text FROM risk.aml_alerts WHERE alert_id = ?", alertId))
                .isEqualTo(createdAt);
        assertThat(Db.queryString("SELECT dedupe_key FROM risk.aml_alerts WHERE alert_id = ?", alertId))
                .isEqualTo("STRUCTURING|" + source + "|2026-03-01T10:00:00Z");

        // D6: dos publicaciones (la alerta nueva y la ampliada), con el mismo alert_id.
        var messages = published(source);
        assertThat(messages).hasSize(2);
        for (var m : messages) {
            assertThat(json(m.value()).at("/payload/alert_id").asText()).isEqualTo(alertId.toString());
        }
        assertThat(json(messages.get(0).value()).at("/payload/transfer_ids")).hasSize(3);
        assertThat(json(messages.get(1).value()).at("/payload/transfer_ids")).hasSize(4);
        assertThat(json(messages.get(1).value()).at("/payload/total_amount").asText()).isEqualTo("38000.00");
    }

    // ---- Reentrega e idempotencia ---------------------------------------------------------------------------

    @Test
    void aRedeliveredEventLeavesNoDuplicateFactNoDuplicateAlertAndNoSecondPublication() {
        UUID source = UUID.randomUUID();
        Events.Transfer big = transfer(source, "12000.00", T0);
        sendAndWait(big);
        // Misma clave: va a la misma partición, así que el marcador se procesa después de la reentrega.
        send(big);
        Events.Transfer marker = transfer(source, "1.00", T0.plus(Duration.ofMinutes(1)));
        sendAndWait(marker);

        assertThat(Db.queryLong("SELECT count(*) FROM risk.transfer_facts WHERE transfer_id = ?", big.transferId)).isEqualTo(1);
        assertThat(alerts(source, "THRESHOLD")).isEqualTo(1);
        assertThat(Db.queryLong("SELECT count(*) FROM risk.processed_messages WHERE event_id = ?", big.eventId)).isEqualTo(1);
        assertThat(published(source)).hasSize(1);

        // Sin el inbox, la reentrega recrearía la alerta borrada: el inbox es lo que la detiene.
        StreamDb.exec("DELETE FROM risk.aml_alerts WHERE subject_account_id = ?", source);
        StreamDb.exec("DELETE FROM risk.transfer_facts WHERE transfer_id = ?", big.transferId);
        send(big);
        sendAndWait(transfer(source, "2.00", T0.plus(Duration.ofMinutes(2))));
        assertThat(alerts(source)).isZero();
        assertThat(Db.queryLong("SELECT count(*) FROM risk.transfer_facts WHERE transfer_id = ?", big.transferId)).isZero();
        assertThat(published(source)).hasSize(1);
    }

    @Test
    void theSameTransferEmittedUnderAnotherEventIdDoesNotDuplicateTheFactOrTheAlert() {
        UUID source = UUID.randomUUID();
        UUID transferId = UUID.randomUUID();
        sendAndWait(transfer(source, "12000.00", T0).transferId(transferId));
        sendAndWait(transfer(source, "12000.00", T0).transferId(transferId));   // otro event_id
        assertThat(Db.queryLong("SELECT count(*) FROM risk.transfer_facts WHERE transfer_id = ?", transferId)).isEqualTo(1);
        assertThat(alerts(source, "THRESHOLD")).isEqualTo(1);
        assertThat(published(source)).hasSize(1);
    }

    // ---- Bordes -----------------------------------------------------------------------------------------------

    @Test
    void thresholdBorder_9999_99DoesNotTriggerAnd10000_00Does() {
        UUID below = UUID.randomUUID();
        UUID exact = UUID.randomUUID();
        Events.Transfer atThreshold = transfer(exact, "10000.00", T0);
        sendAndWait(transfer(below, "9999.99", T0));
        sendAndWait(atThreshold);
        assertThat(alerts(below, "THRESHOLD")).isZero();
        assertThat(alerts(exact, "THRESHOLD")).isEqualTo(1);
        assertThat(Db.queryString("SELECT severity FROM risk.aml_alerts WHERE subject_account_id = ?", exact))
                .isEqualTo("MEDIUM");
        assertThat(Db.queryString("SELECT dedupe_key FROM risk.aml_alerts WHERE subject_account_id = ?", exact))
                .startsWith("THRESHOLD|" + exact + "|");
        // trigger_emitted_at es el emitted_at del evento (a microsegundos).
        assertThat(Db.queryLong("SELECT count(*) FROM risk.aml_alerts WHERE subject_account_id = ? AND trigger_emitted_at = ?",
                exact, java.sql.Timestamp.from(atThreshold.emittedAt.truncatedTo(java.time.temporal.ChronoUnit.MICROS))))
                .isEqualTo(1);
    }

    @Test
    void structuringMinRatioBorder_0_6999DoesNotCountAnd0_70Does() {
        UUID below = UUID.randomUUID();
        UUID at = UUID.randomUUID();
        for (int i = 0; i < 3; i++) {
            sendAndWait(transfer(below, "6999.00", T0.plus(Duration.ofHours(i))));
        }
        for (int i = 0; i < 3; i++) {
            sendAndWait(transfer(at, "7000.00", T0.plus(Duration.ofHours(i))));
        }
        assertThat(alerts(below)).isZero();
        assertThat(alerts(at, "STRUCTURING")).isEqualTo(1);
        assertThat(Db.queryDecimal("SELECT total_amount FROM risk.aml_alerts WHERE subject_account_id = ?", at))
                .isEqualByComparingTo("21000.00");
    }

    @Test
    void structuringMaxRatioIsExclusive_aTransferOfExactlyTheThresholdIsNotPartOfTheSet() {
        UUID source = UUID.randomUUID();
        for (int i = 0; i < 3; i++) {
            sendAndWait(transfer(source, "9500.00", T0.plus(Duration.ofHours(i))));
        }
        // 10.000,00 es ratio 1,00: dispara THRESHOLD pero no entra en el conjunto de fraccionamiento.
        sendAndWait(transfer(source, "10000.00", T0.plus(Duration.ofHours(3))));
        assertThat(alerts(source, "THRESHOLD")).isEqualTo(1);
        assertThat(alerts(source, "STRUCTURING")).isEqualTo(1);
        assertThat(Db.queryLong("SELECT cardinality(transfer_ids) FROM risk.aml_alerts WHERE rule_code = 'STRUCTURING' "
                + "AND subject_account_id = ?", source)).isEqualTo(3);
        assertThat(Db.queryDecimal("SELECT total_amount FROM risk.aml_alerts WHERE rule_code = 'STRUCTURING' "
                + "AND subject_account_id = ?", source)).isEqualByComparingTo("28500.00");
        // 9.999,99 sí está bajo el máximo.
        sendAndWait(transfer(source, "9999.99", T0.plus(Duration.ofHours(4))));
        assertThat(Db.queryLong("SELECT cardinality(transfer_ids) FROM risk.aml_alerts WHERE rule_code = 'STRUCTURING' "
                + "AND subject_account_id = ?", source)).isEqualTo(4);
    }

    @Test
    void structuringWindowBorder_exactly24HoursIsInsideAnd24HoursPlusOneSecondIsOutside() {
        UUID inside = UUID.randomUUID();
        UUID outside = UUID.randomUUID();
        sendAndWait(transfer(inside, "9500.00", T0));
        sendAndWait(transfer(inside, "9500.00", T0.plus(Duration.ofHours(12))));
        sendAndWait(transfer(inside, "9500.00", T0.plus(Duration.ofHours(24))));
        sendAndWait(transfer(outside, "9500.00", T0));
        sendAndWait(transfer(outside, "9500.00", T0.plus(Duration.ofHours(12))));
        sendAndWait(transfer(outside, "9500.00", T0.plus(Duration.ofHours(24)).plusSeconds(1)));
        assertThat(alerts(inside, "STRUCTURING")).isEqualTo(1);
        assertThat(alerts(outside)).isZero();
    }

    @Test
    void structuringNeedsTheMinimumCount() {
        UUID source = UUID.randomUUID();
        sendAndWait(transfer(source, "9500.00", T0));
        sendAndWait(transfer(source, "9500.00", T0.plus(Duration.ofHours(1))));
        assertThat(alerts(source)).isZero();
    }

    @Test
    void countryRiskBorders_highFrom1000AndWatchFrom5000() {
        StreamDb.countryRisk("IR", "HIGH", "2026-01-01", null);
        StreamDb.countryRisk("PA", "WATCH", "2026-01-01", null);
        UUID highBelow = UUID.randomUUID();
        UUID highAt = UUID.randomUUID();
        UUID watchBelow = UUID.randomUUID();
        UUID watchAt = UUID.randomUUID();
        UUID watchAtHighAmount = UUID.randomUUID();
        sendAndWait(transfer(highBelow, "999.99", T0).country("IR"));
        sendAndWait(transfer(highAt, "1000.00", T0).country("IR"));
        sendAndWait(transfer(watchBelow, "4999.99", T0).country("PA"));
        sendAndWait(transfer(watchAt, "5000.00", T0).country("PA"));
        sendAndWait(transfer(watchAtHighAmount, "1000.00", T0).country("PA"));   // 1.000 solo cuenta en nivel HIGH
        sendAndWait(transfer(UUID.randomUUID(), "50000.00", T0).country("ES"));    // país sin riesgo: solo THRESHOLD

        assertThat(alerts(highBelow)).isZero();
        assertThat(alerts(highAt, "COUNTRY_RISK")).isEqualTo(1);
        assertThat(Db.queryString("SELECT severity FROM risk.aml_alerts WHERE subject_account_id = ?", highAt)).isEqualTo("HIGH");
        assertThat(alerts(watchBelow)).isZero();
        assertThat(alerts(watchAt, "COUNTRY_RISK")).isEqualTo(1);
        assertThat(Db.queryString("SELECT severity FROM risk.aml_alerts WHERE subject_account_id = ?", watchAt)).isEqualTo("MEDIUM");
        assertThat(alerts(watchAtHighAmount)).isZero();
    }

    @Test
    void countryListValidityIsEvaluatedAtOccurredAtNotAtNow() {
        // Vigente en [2026-02-01, 2026-03-01): el fin es exclusivo.
        StreamDb.countryRisk("KP", "HIGH", "2026-02-01", "2026-03-01");
        UUID before = UUID.randomUUID();
        UUID first = UUID.randomUUID();
        UUID last = UUID.randomUUID();
        UUID after = UUID.randomUUID();
        sendAndWait(transfer(before, "2000.00", Instant.parse("2026-01-31T23:59:59Z")).country("KP"));
        sendAndWait(transfer(first, "2000.00", Instant.parse("2026-02-01T00:00:00Z")).country("KP"));
        sendAndWait(transfer(last, "2000.00", Instant.parse("2026-02-28T23:59:59Z")).country("KP"));
        sendAndWait(transfer(after, "2000.00", Instant.parse("2026-03-01T00:00:00Z")).country("KP"));
        assertThat(alerts(before)).isZero();
        assertThat(alerts(first, "COUNTRY_RISK")).isEqualTo(1);
        assertThat(alerts(last, "COUNTRY_RISK")).isEqualTo(1);
        assertThat(alerts(after)).isZero();
    }

    // ---- Acumulación y exención -----------------------------------------------------------------------------

    @Test
    void oneTransferCanRaiseThresholdAndCountryRisk() {
        StreamDb.countryRisk("IR", "HIGH", "2026-01-01", null);
        UUID source = UUID.randomUUID();
        sendAndWait(transfer(source, "12000.00", T0).country("IR"));
        assertThat(alerts(source, "THRESHOLD")).isEqualTo(1);
        assertThat(alerts(source, "COUNTRY_RISK")).isEqualTo(1);
        assertThat(published(source)).hasSize(2);
    }

    @Test
    void anExemptSourceProducesNoAlertButIsStillProjected() {
        StreamDb.countryRisk("IR", "HIGH", "2026-01-01", null);
        UUID clearing = UUID.randomUUID();
        StreamDb.exempt(clearing);
        Events.Transfer t = transfer(clearing, "50000.00", T0).country("IR");
        sendAndWait(t);
        for (int i = 0; i < 3; i++) {
            sendAndWait(transfer(clearing, "9500.00", T0.plus(Duration.ofHours(i + 1))));
        }
        assertThat(alerts(clearing)).isZero();
        assertThat(published(clearing)).isEmpty();
        assertThat(Db.queryLong("SELECT count(*) FROM risk.transfer_facts WHERE source_account_id = ?", clearing)).isEqualTo(4);
    }

    // ---- Versiones de las reglas ---------------------------------------------------------------------------

    @Test
    void changingTheActiveVersionChangesTheBehaviourAndNewAlertsRecordVersionAndParams() {
        UUID early = UUID.randomUUID();
        sendAndWait(transfer(early, "12000.00", T0));
        assertThat(Db.queryLong("SELECT rule_version FROM risk.aml_alerts WHERE subject_account_id = ?", early)).isEqualTo(1);
        assertThat(Db.queryString("SELECT rule_params->>'threshold' FROM risk.aml_alerts WHERE subject_account_id = ?", early))
                .isEqualTo("10000.00");

        // Versión 2: umbral 5.000. Un cambio de parámetros es una versión nueva, no un UPDATE de los parámetros.
        StreamDb.exec("UPDATE risk.aml_rules SET active = false WHERE rule_code = 'THRESHOLD' AND version = 1");
        StreamDb.exec("INSERT INTO risk.aml_rules (rule_code, version, params, active, valid_from) "
                + "VALUES ('THRESHOLD', 2, '{\"threshold\": \"5000.00\"}', true, now())");

        // La caché (60 s) sigue en v1 hasta que se refresca: 6.000 aún no dispara.
        UUID cached = UUID.randomUUID();
        sendAndWait(transfer(cached, "6000.00", T0));
        assertThat(alerts(cached)).isZero();

        catalog.refresh().await().atMost(Duration.ofSeconds(10));
        UUID later = UUID.randomUUID();
        sendAndWait(transfer(later, "6000.00", T0));
        assertThat(alerts(later, "THRESHOLD")).isEqualTo(1);
        assertThat(Db.queryLong("SELECT rule_version FROM risk.aml_alerts WHERE subject_account_id = ?", later)).isEqualTo(2);
        assertThat(Db.queryString("SELECT rule_params->>'threshold' FROM risk.aml_alerts WHERE subject_account_id = ?", later))
                .isEqualTo("5000.00");
        // La alerta anterior conserva la versión y los parámetros con que se disparó.
        assertThat(Db.queryLong("SELECT rule_version FROM risk.aml_alerts WHERE subject_account_id = ?", early)).isEqualTo(1);
    }

    @Test
    void aRuleWithoutAnActiveVersionIsSkipped() {
        StreamDb.exec("UPDATE risk.aml_rules SET active = false WHERE rule_code = 'THRESHOLD'");
        catalog.refresh().await().atMost(Duration.ofSeconds(10));
        UUID source = UUID.randomUUID();
        sendAndWait(transfer(source, "50000.00", T0));
        assertThat(alerts(source)).isZero();
    }

    // ---- Extensibilidad de la cadena -----------------------------------------------------------------------

    @Test
    void aNewRuleJoinsTheChainWithoutTouchingTheExistingOnes() {
        assertThat(chain.rules()).extracting(AmlRule::order).isSorted();
        assertThat(chain.rules()).anyMatch(r -> r instanceof FictitiousRule);

        FictitiousRule.enabled = true;
        UUID source = UUID.randomUUID();
        sendAndWait(transfer(source, "777.77", T0));
        assertThat(alerts(source, "FICTITIOUS")).isEqualTo(1);

        // Las reglas existentes siguen igual y la exención sigue cortando también a la nueva.
        UUID exempt = UUID.randomUUID();
        StreamDb.exempt(exempt);
        sendAndWait(transfer(exempt, "777.77", T0));
        assertThat(alerts(exempt)).isZero();
        sendAndWait(transfer(source, "12000.00", T0.plusSeconds(1)));
        assertThat(alerts(source, "THRESHOLD")).isEqualTo(1);
    }

    // ---- Tiempo de negocio ----------------------------------------------------------------------------------

    @Test
    void windowsUseTheBusinessTimeOfTheEventNotTheWallClock() {
        UUID source = UUID.randomUUID();
        // Tres transferencias de hace años, procesadas ahora: el fraccionamiento se evalúa con occurred_at.
        Instant past = Instant.parse("2019-06-01T08:00:00Z");
        for (int i = 0; i < 3; i++) {
            sendAndWait(transfer(source, "9500.00", past.plus(Duration.ofHours(i))));
        }
        assertThat(alerts(source, "STRUCTURING")).isEqualTo(1);
        assertThat(Db.queryString("SELECT to_char(window_start AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS') "
                + "FROM risk.aml_alerts WHERE subject_account_id = ?", source)).isEqualTo("2019-06-01T08:00:00");
        // Y una de ahora no se mezcla con ellas.
        sendAndWait(transfer(source, "9500.00", Instant.now()));
        assertThat(Db.queryLong("SELECT cardinality(transfer_ids) FROM risk.aml_alerts WHERE subject_account_id = ?", source))
                .isEqualTo(3);
    }
}
