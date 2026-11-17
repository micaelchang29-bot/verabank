package com.verabank.risk.scoring;

import static io.restassured.RestAssured.given;
import static org.assertj.core.api.Assertions.assertThat;
import static org.hamcrest.Matchers.containsString;
import static org.hamcrest.Matchers.matchesPattern;
import static org.hamcrest.Matchers.not;

import com.verabank.risk.support.Db;
import com.verabank.risk.support.PgInfra;
import com.verabank.risk.support.Requests;
import io.quarkus.test.junit.QuarkusTest;
import io.restassured.http.ContentType;
import io.restassured.response.Response;
import java.math.BigDecimal;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

@QuarkusTest
class ScoreEndpointIT {

    private static final String UUID_PATTERN = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";

    @BeforeEach
    void clean() {
        PgInfra.truncateLog();
    }

    private static Response post(Object body) {
        return given().contentType(ContentType.JSON).body(body).post("/internal/v1/scores");
    }

    @Test
    void aValidRequestReturnsScoreDecisionAndModelVersionAndLogsOneRow() {
        UUID id = UUID.randomUUID();
        Response response = post(Requests.valid(id)).then().statusCode(200).contentType(ContentType.JSON).extract().response();
        BigDecimal score = new BigDecimal(response.jsonPath().getString("score"));
        assertThat(score).isBetween(BigDecimal.ZERO, BigDecimal.ONE);
        assertThat(score.scale()).isLessThanOrEqualTo(5);
        assertThat(response.jsonPath().getString("decision")).isIn("APPROVE", "REVIEW", "BLOCK");
        assertThat(response.jsonPath().getString("model_version")).matches("fraud-(xgb|logreg)/\\d+");
        assertThat(response.header("X-Correlation-Id")).matches(UUID_PATTERN);

        assertThat(Db.count(id)).isEqualTo(1);
        assertThat(Db.queryDecimal("SELECT score FROM risk.scoring_log WHERE transfer_id = ?", id)).isEqualByComparingTo(score);
        assertThat(Db.queryString("SELECT decision FROM risk.scoring_log WHERE transfer_id = ?", id))
                .isEqualTo(response.jsonPath().getString("decision"));
        assertThat(Db.queryString("SELECT model_version FROM risk.scoring_log WHERE transfer_id = ?", id))
                .isEqualTo(response.jsonPath().getString("model_version"));
        assertThat(Db.queryString("SELECT source_ref || '|' || dest_ref || '|' || amount FROM risk.scoring_log WHERE transfer_id = ?", id))
                .isEqualTo("src-01|dst-01|1250.00");
        // features: el JSONB lleva los 17 nombres del contrato
        assertThat(Db.queryLong("SELECT count(*) FROM risk.scoring_log, jsonb_object_keys(features) WHERE transfer_id = ?", id))
                .isEqualTo(17);
        assertThat(Db.queryString("SELECT shadow_score FROM risk.scoring_log WHERE transfer_id = ?", id)).isNull();
    }

    @Test
    void correlationIdIsEchoedWhenItIsAUuidAndReplacedWhenItIsNot() {
        String mine = UUID.randomUUID().toString();
        given().contentType(ContentType.JSON).header("X-Correlation-Id", mine).body(Requests.valid(UUID.randomUUID()))
                .post("/internal/v1/scores").then().statusCode(200).header("X-Correlation-Id", mine);
        String replaced = given().contentType(ContentType.JSON).header("X-Correlation-Id", "no-es-un-uuid")
                .body(Requests.valid(UUID.randomUUID())).post("/internal/v1/scores").then().statusCode(200)
                .header("X-Correlation-Id", matchesPattern(UUID_PATTERN)).extract().header("X-Correlation-Id");
        assertThat(replaced).isNotEqualTo("no-es-un-uuid");
    }

    @Test
    void invalidBodiesAreValidationErrorsAsProblemJson() {
        List<Map<String, Object>> bodies = new ArrayList<>();
        UUID id = UUID.randomUUID();
        for (String missing : List.of("transfer_id", "source_ref", "dest_ref", "amount", "currency", "transfer_type",
                "dest_country", "occurred_at")) {
            bodies.add(Requests.valid(id, b -> b.remove(missing)));
        }
        bodies.add(Requests.valid(id, b -> b.put("transfer_id", "no-uuid")));
        bodies.add(Requests.valid(id, b -> b.put("source_ref", "")));
        bodies.add(Requests.valid(id, b -> b.put("dest_ref", "")));
        bodies.add(Requests.valid(id, b -> b.put("amount", "12.345")));
        bodies.add(Requests.valid(id, b -> b.put("amount", "-5.00")));
        bodies.add(Requests.valid(id, b -> b.put("amount", 1250.0)));
        bodies.add(Requests.valid(id, b -> b.put("currency", "pen")));
        bodies.add(Requests.valid(id, b -> b.put("transfer_type", "CASH_IN")));
        bodies.add(Requests.valid(id, b -> b.put("dest_country", "PER")));
        bodies.add(Requests.valid(id, b -> b.put("occurred_at", "ayer")));
        bodies.add(Requests.valid(id, b -> b.put("occurred_at", "2026-01-10T10:00:00")));
        for (Map<String, Object> body : bodies) {
            post(body).then().statusCode(400).contentType("application/problem+json")
                    .body("type", org.hamcrest.Matchers.equalTo("validation-error"))
                    .body("status", org.hamcrest.Matchers.equalTo(400))
                    .body("title", not(org.hamcrest.Matchers.emptyOrNullString()))
                    .body("detail", not(org.hamcrest.Matchers.emptyOrNullString()))
                    .body("correlation_id", matchesPattern(UUID_PATTERN));
        }
        assertThat(Db.queryLong("SELECT count(*) FROM risk.scoring_log")).isZero();
    }

    @Test
    void malformedJsonAnEmptyBodyAndANonObjectAreValidationErrors() {
        for (String body : List.of("{no es json", "", "[]", "\"texto\"", "null")) {
            given().contentType(ContentType.JSON).body(body).post("/internal/v1/scores").then().statusCode(400)
                    .contentType("application/problem+json").body("type", org.hamcrest.Matchers.equalTo("validation-error"));
        }
    }

    @Test
    void otherMethodsAndPathsAreProblemJson() {
        given().get("/internal/v1/scores").then().statusCode(405).contentType("application/problem+json")
                .body("type", org.hamcrest.Matchers.equalTo("method-not-allowed")).body("correlation_id", matchesPattern(UUID_PATTERN));
        given().get("/internal/v1/no-existe").then().statusCode(404).contentType("application/problem+json")
                .body("type", org.hamcrest.Matchers.equalTo("not-found"));
    }

    @Test
    void aRetryReturnsTheStoredResultWithoutRecomputing() {
        UUID id = UUID.randomUUID();
        Map<String, Object> body = Requests.valid(id);
        Response first = post(body).then().statusCode(200).extract().response();
        // Si se recalculara, el historial nuevo cambiaría las features (y con ellas el score).
        for (int i = 1; i <= 5; i++) {
            Db.insertHistory("src-01", "dst-01", "9999.00", Instant.parse("2026-01-10T09:00:00Z").minusSeconds(60L * i), "warmup");
        }
        for (int i = 0; i < 3; i++) {
            Response again = post(body).then().statusCode(200).extract().response();
            assertThat(again.jsonPath().getString("score")).isEqualTo(first.jsonPath().getString("score"));
            assertThat(again.jsonPath().getString("decision")).isEqualTo(first.jsonPath().getString("decision"));
            assertThat(again.jsonPath().getString("model_version")).isEqualTo(first.jsonPath().getString("model_version"));
        }
        assertThat(Db.count(id)).isEqualTo(1);
    }

    @Test
    void twentyConcurrentRequestsWithTheSameTransferIdReturnTheSameAndLeaveOneRow() throws Exception {
        UUID id = UUID.randomUUID();
        Map<String, Object> body = Requests.valid(id);
        ExecutorService pool = Executors.newFixedThreadPool(20);
        CountDownLatch start = new CountDownLatch(1);
        List<Future<List<String>>> results = new ArrayList<>();
        for (int i = 0; i < 20; i++) {
            results.add(pool.submit(() -> {
                start.await();
                Response r = post(body);
                return List.of(String.valueOf(r.statusCode()), r.jsonPath().getString("score"),
                        r.jsonPath().getString("decision"), r.jsonPath().getString("model_version"));
            }));
        }
        start.countDown();
        List<List<String>> all = new ArrayList<>();
        for (Future<List<String>> f : results) {
            all.add(f.get());
        }
        pool.shutdown();
        assertThat(all).hasSize(20).allSatisfy(r -> assertThat(r.get(0)).isEqualTo("200"));
        assertThat(all.stream().distinct().toList()).hasSize(1);
        assertThat(Db.count(id)).isEqualTo(1);
        assertThat(Db.queryDecimal("SELECT score FROM risk.scoring_log WHERE transfer_id = ?", id))
                .isEqualByComparingTo(new BigDecimal(all.get(0).get(1)));
    }

    @Test
    void warmupRowsCountAsHistoryForTheFeatures() {
        Instant t = Instant.parse("2026-01-10T10:00:00Z");
        Db.insertHistory("src-02", "dst-01", "40.00", t.minusSeconds(3600), "warmup");
        Db.insertHistory("src-03", "dst-01", "60.00", t.minusSeconds(7200), "warmup");
        UUID id = UUID.randomUUID();
        post(Requests.valid(id)).then().statusCode(200);
        assertThat(Db.queryDecimal("SELECT (features->>'dst_in_count_24h')::numeric FROM risk.scoring_log WHERE transfer_id = ?", id))
                .isEqualByComparingTo("2");
        assertThat(Db.queryDecimal("SELECT (features->>'dst_in_sum_24h')::numeric FROM risk.scoring_log WHERE transfer_id = ?", id))
                .isEqualByComparingTo("100");
        assertThat(Db.queryDecimal("SELECT (features->>'dst_age_hours')::numeric FROM risk.scoring_log WHERE transfer_id = ?", id))
                .isEqualByComparingTo("2");
    }

    @Test
    void readinessIsUpAndMetricsExposeStagesDecisionsAndErrors() {
        post(Requests.valid(UUID.randomUUID())).then().statusCode(200);
        post("{}").then().statusCode(400);
        given().get("/q/health/ready").then().statusCode(200).body("status", org.hamcrest.Matchers.equalTo("UP"));
        given().get("/q/health/live").then().statusCode(200);
        String metrics = given().get("/q/metrics").then().statusCode(200).extract().asString();
        for (String stage : List.of("features", "inference", "log", "total")) {
            assertThat(metrics).contains("scoring_stage_duration_seconds_bucket{stage=\"" + stage + "\"");
            assertThat(metrics).contains("scoring_stage_duration_seconds_count{stage=\"" + stage + "\"");
        }
        assertThat(metrics).contains("scoring_decisions_total{decision=\"");
        assertThat(metrics).contains("scoring_errors_total{type=\"validation\"");
        assertThat(metrics).contains("scoring_shadow_failures_total");
        // El presupuesto de §1.2 son cubetas: 40 ms para features, 10 ms para inferencia y bitácora.
        assertThat(metrics).contains("scoring_stage_duration_seconds_bucket{stage=\"features\",le=\"0.04\"");
        assertThat(metrics).contains("scoring_stage_duration_seconds_bucket{stage=\"inference\",le=\"0.01\"");
    }
}
