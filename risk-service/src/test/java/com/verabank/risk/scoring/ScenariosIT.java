package com.verabank.risk.scoring;

import static io.restassured.RestAssured.given;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.verabank.risk.support.Dirs;
import com.verabank.risk.support.PgInfra;
import io.quarkus.test.junit.QuarkusTest;
import io.restassured.http.ContentType;
import io.restassured.response.Response;
import java.nio.file.Files;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.junit.jupiter.api.Test;

/**
 * T-13 (Java), parte (c): los escenarios de contracts/fixtures/feature_scenarios.json, reproducidos en orden contra
 * PostgreSQL real a través del servicio. Las features guardadas en la bitácora coinciden a 1e-9 con las esperadas
 * (que Python calculó con reference.py y verificó contra features.py) y el score, a 1e-5.
 */
@QuarkusTest
class ScenariosIT {

    private static final double FEATURE_TOLERANCE = 1e-9;
    private static final double SCORE_TOLERANCE = 1e-5;

    @Test
    void everyScenarioReproducesItsExpectedFeaturesAndScore() throws Exception {
        JsonNode fixtures = new ObjectMapper().readTree(Files.readAllBytes(Dirs.contracts().resolve("fixtures/feature_scenarios.json")));
        List<String> order = new ArrayList<>();
        fixtures.get("feature_order").forEach(n -> order.add(n.asText()));
        assertThat(order).isEqualTo(FeatureComputer.FEATURE_NAMES);
        assertThat(fixtures.get("scenarios").size()).isGreaterThanOrEqualTo(12);

        int events = 0;
        double worstFeature = 0;
        double worstScore = 0;
        for (JsonNode scenario : fixtures.get("scenarios")) {
            PgInfra.truncateLog();   // cada escenario empieza sin historia
            String name = scenario.get("name").asText();
            for (JsonNode event : scenario.get("events")) {
                UUID id = UUID.randomUUID();
                Map<String, Object> body = new LinkedHashMap<>();
                body.put("transfer_id", id.toString());
                body.put("source_ref", event.get("source_ref").asText());
                body.put("dest_ref", event.get("dest_ref").asText());
                body.put("amount", event.get("amount").asText());
                body.put("currency", "PEN");
                body.put("transfer_type", event.get("transfer_type").asText());
                body.put("dest_country", "PE");
                body.put("occurred_at", event.get("occurred_at").asText());
                Response response = given().contentType(ContentType.JSON).body(body).post("/internal/v1/scores");
                String where = name + " / " + event.get("occurred_at").asText() + " " + event.get("source_ref").asText();
                assertThat(response.statusCode()).as(where).isEqualTo(200);

                Map<String, Double> stored = storedFeatures(id);
                assertThat(stored.keySet()).as(where).containsExactlyInAnyOrderElementsOf(order);
                for (String feature : order) {
                    double expected = event.get("expected_features").get(feature).asDouble();
                    double diff = Math.abs(stored.get(feature) - expected);
                    worstFeature = Math.max(worstFeature, diff / Math.max(1.0, Math.abs(expected)));
                    assertThat(stored.get(feature)).as(where + " · " + feature)
                            .isCloseTo(expected, org.assertj.core.data.Offset.offset(FEATURE_TOLERANCE * Math.max(1.0, Math.abs(expected))));
                }
                double score = response.jsonPath().getDouble("score");
                double expectedScore = event.get("expected_score").asDouble();
                worstScore = Math.max(worstScore, Math.abs(score - expectedScore));
                assertThat(score).as(where + " · score").isCloseTo(expectedScore, org.assertj.core.data.Offset.offset(SCORE_TOLERANCE));
                events++;
            }
        }
        assertThat(events).isGreaterThanOrEqualTo(60);
        assertThat(worstFeature).isLessThan(FEATURE_TOLERANCE);
        assertThat(worstScore).isLessThan(SCORE_TOLERANCE);
    }

    private static Map<String, Double> storedFeatures(UUID id) throws Exception {
        try (Connection c = PgInfra.admin(); PreparedStatement ps = c.prepareStatement(
                "SELECT k, (features->>k)::float8 FROM risk.scoring_log, jsonb_object_keys(features) AS k WHERE transfer_id = ?")) {
            ps.setObject(1, id);
            Map<String, Double> out = new LinkedHashMap<>();
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) {
                    out.put(rs.getString(1), rs.getDouble(2));
                }
            }
            return out;
        }
    }
}
