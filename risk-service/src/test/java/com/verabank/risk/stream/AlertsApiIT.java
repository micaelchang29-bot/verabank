package com.verabank.risk.stream;

import static io.restassured.RestAssured.given;
import static org.assertj.core.api.Assertions.assertThat;
import static org.hamcrest.Matchers.equalTo;
import static org.hamcrest.Matchers.hasSize;
import static org.hamcrest.Matchers.nullValue;

import com.verabank.risk.support.Db;
import io.quarkus.test.junit.QuarkusTest;
import io.quarkus.test.junit.TestProfile;
import io.restassured.http.ContentType;
import io.restassured.response.Response;
import io.restassured.specification.RequestSpecification;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/** GET /api/v1/aml/alerts y PATCH /api/v1/aml/alerts/{id} (D4). */
@QuarkusTest
@TestProfile(StreamProfile.class)
class AlertsApiIT {

    private static final Instant BASE = Instant.parse("2026-03-01T10:00:00Z");

    @BeforeEach
    void reset() {
        StreamDb.reset();
    }

    private static RequestSpecification ops() {
        return given().header("X-API-Key", StreamProfile.KEY_OPS);
    }

    private static UUID alert(String rule, String severity, String status, int minutesAfterBase) {
        UUID id = UUID.randomUUID();
        StreamDb.exec("""
                INSERT INTO risk.aml_alerts (alert_id, dedupe_key, rule_code, rule_version, rule_params, subject_account_id,
                  severity, status, window_start, window_end, total_amount, transfer_ids, trigger_emitted_at, created_at)
                VALUES (?, ?, ?, 1, '{"threshold": "10000.00"}', ?, ?, ?, ?, ?, 12000.00, ARRAY[?]::uuid[], ?, ?)""",
                id, "k|" + id, rule, UUID.randomUUID(), severity, status, Timestamp.from(BASE), Timestamp.from(BASE),
                UUID.randomUUID(), Timestamp.from(BASE), Timestamp.from(BASE.plusSeconds(60L * minutesAfterBase)));
        return id;
    }

    // ---- API key -------------------------------------------------------------------------------------------

    @Test
    void withoutAValidApiKeyEverythingIs401() {
        given().get("/api/v1/aml/alerts").then().statusCode(401).contentType("application/problem+json")
                .body("type", equalTo("unauthorized")).body("status", equalTo(401))
                .header("WWW-Authenticate", "ApiKey header=\"X-API-Key\"");
        given().header("X-API-Key", "clave-que-no-existe-0000").get("/api/v1/aml/alerts").then().statusCode(401)
                .body("type", equalTo("unauthorized"));
        UUID id = alert("THRESHOLD", "MEDIUM", "OPEN", 0);
        given().contentType(ContentType.JSON).body("{\"status\":\"IN_REVIEW\"}").patch("/api/v1/aml/alerts/" + id)
                .then().statusCode(401).body("type", equalTo("unauthorized"));
        assertThat(Db.queryString("SELECT status FROM risk.aml_alerts WHERE alert_id = ?", id)).isEqualTo("OPEN");
    }

    // ---- Lectura -------------------------------------------------------------------------------------------

    @Test
    void listFiltersByStatusSeverityAndRuleCode() {
        alert("THRESHOLD", "MEDIUM", "OPEN", 0);
        alert("STRUCTURING", "HIGH", "OPEN", 1);
        alert("COUNTRY_RISK", "HIGH", "IN_REVIEW", 2);
        alert("COUNTRY_RISK", "MEDIUM", "CLOSED_CONFIRMED", 3);

        ops().get("/api/v1/aml/alerts").then().statusCode(200).body("items", hasSize(4)).body("next_cursor", nullValue());
        ops().get("/api/v1/aml/alerts?status=OPEN").then().body("items", hasSize(2));
        ops().get("/api/v1/aml/alerts?severity=HIGH").then().body("items", hasSize(2));
        ops().get("/api/v1/aml/alerts?rule_code=COUNTRY_RISK").then().body("items", hasSize(2));
        ops().get("/api/v1/aml/alerts?status=OPEN&severity=HIGH&rule_code=STRUCTURING").then()
                .body("items", hasSize(1)).body("items[0].rule_code", equalTo("STRUCTURING"));
        ops().get("/api/v1/aml/alerts?rule_code=NO_EXISTE").then().body("items", hasSize(0));
    }

    @Test
    void anAlertHasTheContractFields() {
        UUID id = alert("THRESHOLD", "MEDIUM", "OPEN", 0);
        ops().get("/api/v1/aml/alerts").then().statusCode(200)
                .body("items[0].alert_id", equalTo(id.toString()))
                .body("items[0].rule_code", equalTo("THRESHOLD"))
                .body("items[0].rule_version", equalTo(1))
                .body("items[0].rule_params.threshold", equalTo("10000.00"))
                .body("items[0].severity", equalTo("MEDIUM"))
                .body("items[0].status", equalTo("OPEN"))
                .body("items[0].total_amount", equalTo("12000.00"))
                .body("items[0].transfer_ids", hasSize(1))
                .body("items[0].window_start", equalTo("2026-03-01T10:00:00Z"))
                .body("items[0].created_at", equalTo("2026-03-01T10:00:00Z"))
                .body("items[0].closed_at", nullValue())
                .body("items[0].closed_by", nullValue());
    }

    @Test
    void invalidQueryParametersAre400() {
        for (String query : List.of("status=NOPE", "severity=EXTREME", "limit=0", "limit=201", "limit=abc",
                "cursor=%25%25%25", "cursor=" + java.util.Base64.getUrlEncoder().withoutPadding()
                        .encodeToString("9|2026-03-01T10:00:00Z|x".getBytes()))) {
            ops().get("/api/v1/aml/alerts?" + query).then().statusCode(400).contentType("application/problem+json")
                    .body("type", equalTo("validation-error"));
        }
    }

    @Test
    void theCursorWalksTheQueueBySeverityThenOldestFirstWithoutGapsOrRepeats() {
        List<UUID> expected = new ArrayList<>();
        UUID highOld = alert("STRUCTURING", "HIGH", "OPEN", 0);
        UUID mediumA = alert("THRESHOLD", "MEDIUM", "OPEN", 1);
        UUID lowA = alert("THRESHOLD", "LOW", "OPEN", 2);
        UUID highNew = alert("STRUCTURING", "HIGH", "OPEN", 3);
        UUID mediumB = alert("THRESHOLD", "MEDIUM", "OPEN", 4);
        UUID highMid = alert("COUNTRY_RISK", "HIGH", "OPEN", 2);
        UUID lowB = alert("THRESHOLD", "LOW", "OPEN", 5);
        expected.addAll(List.of(highOld, highMid, highNew, mediumA, mediumB, lowA, lowB));

        List<String> seen = new ArrayList<>();
        String cursor = null;
        int pages = 0;
        do {
            Response page = ops().get("/api/v1/aml/alerts?limit=3" + (cursor == null ? "" : "&cursor=" + cursor));
            page.then().statusCode(200);
            List<String> ids = page.jsonPath().getList("items.alert_id");
            assertThat(ids.size()).isLessThanOrEqualTo(3);
            seen.addAll(ids);
            cursor = page.jsonPath().getString("next_cursor");
            pages++;
        } while (cursor != null);

        assertThat(pages).isEqualTo(3);
        assertThat(seen).containsExactlyElementsOf(expected.stream().map(UUID::toString).toList());
    }

    @Test
    void theCursorKeepsWorkingWhenFiltersAreApplied() {
        for (int i = 0; i < 5; i++) {
            alert("THRESHOLD", "MEDIUM", "OPEN", i);
        }
        alert("STRUCTURING", "HIGH", "OPEN", 0);
        Response first = ops().get("/api/v1/aml/alerts?rule_code=THRESHOLD&limit=2");
        assertThat(first.jsonPath().getList("items")).hasSize(2);
        Response second = ops().get("/api/v1/aml/alerts?rule_code=THRESHOLD&limit=2&cursor=" + first.jsonPath().getString("next_cursor"));
        Response third = ops().get("/api/v1/aml/alerts?rule_code=THRESHOLD&limit=2&cursor=" + second.jsonPath().getString("next_cursor"));
        assertThat(third.jsonPath().getList("items")).hasSize(1);
        assertThat(third.jsonPath().getString("next_cursor")).isNull();
    }

    // ---- Transiciones --------------------------------------------------------------------------------------

    private static Response patch(RequestSpecification spec, UUID id, String body) {
        return spec.contentType(ContentType.JSON).body(body).patch("/api/v1/aml/alerts/" + id);
    }

    @Test
    void validTransitionsAreAppliedClosingSetsClosedAtAndClosedByAndEachChangeIsAudited() {
        UUID id = alert("STRUCTURING", "HIGH", "OPEN", 0);
        UUID correlation = UUID.randomUUID();

        patch(ops().header("X-Correlation-Id", correlation.toString()), id, "{\"status\":\"IN_REVIEW\",\"comment\":\"tomada\"}")
                .then().statusCode(200).body("status", equalTo("IN_REVIEW")).body("closed_at", nullValue())
                .body("closed_by", nullValue()).body("alert_id", equalTo(id.toString()));
        patch(ops(), id, "{\"status\":\"ESCALATED\"}").then().statusCode(200).body("status", equalTo("ESCALATED"));
        Response closed = patch(given().header("X-API-Key", StreamProfile.KEY_COMPLIANCE), id,
                "{\"status\":\"CLOSED_CONFIRMED\",\"comment\":\"confirmada por cumplimiento\"}");
        closed.then().statusCode(200).body("status", equalTo("CLOSED_CONFIRMED")).body("closed_by", equalTo("compliance"));
        assertThat(closed.jsonPath().getString("closed_at")).isNotNull();
        assertThat(Db.queryString("SELECT closed_by FROM risk.aml_alerts WHERE alert_id = ?", id)).isEqualTo("compliance");

        // Auditoría: una fila por cambio, con el canal como actor y el comentario en el detalle.
        assertThat(Db.queryLong("SELECT count(*) FROM audit.audit_log WHERE entity_type = 'AML_ALERT' AND entity_id = ?",
                id.toString())).isEqualTo(3);
        assertThat(Db.queryString("SELECT actor FROM audit.audit_log WHERE entity_id = ? ORDER BY audit_id LIMIT 1", id.toString()))
                .isEqualTo("ops");
        assertThat(Db.queryString("SELECT actor FROM audit.audit_log WHERE entity_id = ? ORDER BY audit_id DESC LIMIT 1", id.toString()))
                .isEqualTo("compliance");
        assertThat(Db.queryString("SELECT action FROM audit.audit_log WHERE entity_id = ? LIMIT 1", id.toString()))
                .isEqualTo("AML_ALERT_STATUS_CHANGED");
        assertThat(Db.queryString("SELECT detail->>'comment' FROM audit.audit_log WHERE entity_id = ? ORDER BY audit_id LIMIT 1", id.toString()))
                .isEqualTo("tomada");
        assertThat(Db.queryString("SELECT detail->>'from' || '>' || (detail->>'to') FROM audit.audit_log WHERE entity_id = ? ORDER BY audit_id DESC LIMIT 1", id.toString()))
                .isEqualTo("ESCALATED>CLOSED_CONFIRMED");
        assertThat(Db.queryString("SELECT correlation_id::text FROM audit.audit_log WHERE entity_id = ? ORDER BY audit_id LIMIT 1", id.toString()))
                .isEqualTo(correlation.toString());
        assertAuditChainIntact();
    }

    @Test
    void closingFromInReviewDirectlyAsFalsePositiveWorks() {
        UUID id = alert("THRESHOLD", "MEDIUM", "IN_REVIEW", 0);
        patch(ops(), id, "{\"status\":\"CLOSED_FALSE_POSITIVE\"}").then().statusCode(200)
                .body("status", equalTo("CLOSED_FALSE_POSITIVE")).body("closed_by", equalTo("ops"));
    }

    @Test
    void invalidTransitionsAre409AndChangeNothingNorAudit() {
        record Case(String from, String to) {
        }
        List<Case> cases = List.of(new Case("OPEN", "OPEN"), new Case("OPEN", "ESCALATED"),
                new Case("OPEN", "CLOSED_FALSE_POSITIVE"), new Case("OPEN", "CLOSED_CONFIRMED"),
                new Case("IN_REVIEW", "OPEN"), new Case("IN_REVIEW", "IN_REVIEW"), new Case("ESCALATED", "OPEN"),
                new Case("ESCALATED", "IN_REVIEW"), new Case("ESCALATED", "ESCALATED"),
                new Case("CLOSED_CONFIRMED", "OPEN"), new Case("CLOSED_CONFIRMED", "IN_REVIEW"),
                new Case("CLOSED_CONFIRMED", "CLOSED_FALSE_POSITIVE"),
                new Case("CLOSED_FALSE_POSITIVE", "CLOSED_CONFIRMED"));
        long auditBefore = Db.queryLong("SELECT count(*) FROM audit.audit_log");
        for (Case c : cases) {
            UUID id = alert("THRESHOLD", "MEDIUM", c.from(), 0);
            patch(ops(), id, "{\"status\":\"" + c.to() + "\"}").then().statusCode(409)
                    .contentType("application/problem+json").body("type", equalTo("alert-transition-invalid"))
                    .body("status", equalTo(409));
            assertThat(Db.queryString("SELECT status FROM risk.aml_alerts WHERE alert_id = ?", id)).as("%s→%s", c.from(), c.to())
                    .isEqualTo(c.from());
        }
        assertThat(Db.queryLong("SELECT count(*) FROM audit.audit_log")).isEqualTo(auditBefore);
    }

    @Test
    void malformedRequestsAre400AndAnUnknownAlertIs404() {
        UUID id = alert("THRESHOLD", "MEDIUM", "OPEN", 0);
        for (String body : List.of("{}", "no es json", "[]", "{\"status\":\"NOPE\"}", "{\"status\":7}",
                "{\"status\":\"IN_REVIEW\",\"comment\":5}",
                "{\"status\":\"IN_REVIEW\",\"comment\":\"" + "x".repeat(1001) + "\"}")) {
            patch(ops(), id, body).then().statusCode(400).body("type", equalTo("validation-error"));
        }
        given().header("X-API-Key", StreamProfile.KEY_OPS).contentType(ContentType.JSON)
                .body("{\"status\":\"IN_REVIEW\"}").patch("/api/v1/aml/alerts/no-es-uuid").then().statusCode(400)
                .body("type", equalTo("validation-error"));
        patch(ops(), UUID.randomUUID(), "{\"status\":\"IN_REVIEW\"}").then().statusCode(404)
                .contentType("application/problem+json").body("type", equalTo("alert-not-found"));
        assertThat(Db.queryString("SELECT status FROM risk.aml_alerts WHERE alert_id = ?", id)).isEqualTo("OPEN");
    }

    @Test
    void twoConcurrentClosuresLeaveExactlyOneWinner() throws Exception {
        UUID id = alert("THRESHOLD", "MEDIUM", "IN_REVIEW", 0);
        CompletableFuture<Integer> a = CompletableFuture.supplyAsync(
                () -> patch(ops(), id, "{\"status\":\"CLOSED_CONFIRMED\"}").statusCode());
        CompletableFuture<Integer> b = CompletableFuture.supplyAsync(
                () -> patch(ops(), id, "{\"status\":\"CLOSED_FALSE_POSITIVE\"}").statusCode());
        assertThat(List.of(a.get(), b.get())).containsExactlyInAnyOrder(200, 409);
        assertThat(Db.queryLong("SELECT count(*) FROM audit.audit_log WHERE entity_id = ?", id.toString())).isEqualTo(1);
        assertAuditChainIntact();
    }

    private static void assertAuditChainIntact() {
        // prev_hash apunta al row_hash anterior y row_hash se recalcula con la fórmula de V11.
        assertThat(Db.queryLong("""
                SELECT count(*) FROM audit.audit_log a LEFT JOIN audit.audit_log p ON p.audit_id = a.audit_id - 1
                 WHERE a.prev_hash IS DISTINCT FROM p.row_hash""")).isZero();
        assertThat(Db.queryLong("""
                SELECT count(*) FROM audit.audit_log a
                 WHERE a.row_hash IS DISTINCT FROM sha256(coalesce(a.prev_hash, ''::bytea) || convert_to(concat_ws('|',
                       to_char(a.occurred_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
                       a.actor, a.action, a.entity_type, a.entity_id, a.detail::text), 'UTF8'))""")).isZero();
        assertThat(Db.queryLong("SELECT count(*) FROM audit.audit_log")).isPositive();
    }

    // ---- De punta a punta ----------------------------------------------------------------------------------

    @Test
    void anAlertRaisedFromAnEventShowsUpInTheQueueAndCanBeReviewed() {
        UUID source = UUID.randomUUID();
        StreamSupport.sendAndWait(Events.transfer(source, "15000.00", StreamSupport.T0));
        Response list = ops().get("/api/v1/aml/alerts?rule_code=THRESHOLD&status=OPEN");
        list.then().statusCode(200);
        assertThat(list.jsonPath().getList("items.subject_account_id")).contains(source.toString());
        String id = list.jsonPath().getString("items.find { it.subject_account_id == '" + source + "' }.alert_id");
        patch(ops(), UUID.fromString(id), "{\"status\":\"IN_REVIEW\"}").then().statusCode(200);
    }
}
