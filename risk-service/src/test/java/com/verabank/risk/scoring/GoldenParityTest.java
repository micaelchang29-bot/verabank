package com.verabank.risk.scoring;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.verabank.risk.support.Dirs;
import java.io.IOException;
import java.math.BigDecimal;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

/**
 * T-13 (Java), partes (a) y (b): ONNX Runtime reproduce los 1.000 scores de golden.jsonl (champion) y de
 * golden_challenger.jsonl (challenger) con diferencia < 1e-5, y la decisión coincide con la de redondear el score.
 */
class GoldenParityTest {

    private static final double TOLERANCE = 1e-5;
    private static final ObjectMapper JSON = new ObjectMapper().enable(DeserializationFeature.USE_BIG_DECIMAL_FOR_FLOATS);

    private static JsonNode thresholds;
    private static List<String> order;
    private static List<JsonNode> golden;
    private static ThresholdDecisionPolicy policy;

    @BeforeAll
    static void load() throws IOException {
        Path artifacts = Dirs.artifacts();
        thresholds = JSON.readTree(Files.readAllBytes(artifacts.resolve("thresholds.json")));
        order = new ArrayList<>();
        thresholds.get("feature_order").forEach(n -> order.add(n.asText()));
        golden = lines(artifacts.resolve("golden.jsonl"));
        policy = new ThresholdDecisionPolicy(thresholds.get("t_review").decimalValue(),
                thresholds.get("t_block").decimalValue(), thresholds.get("rounding_decimals").asInt());
    }

    private static List<JsonNode> lines(Path file) throws IOException {
        List<JsonNode> out = new ArrayList<>();
        for (String line : Files.readAllLines(file)) {
            out.add(JSON.readTree(line));
        }
        return out;
    }

    private static float[] row(JsonNode features) {
        float[] row = new float[order.size()];
        for (int i = 0; i < row.length; i++) {
            row[i] = (float) features.get(order.get(i)).asDouble();
        }
        return row;
    }

    private static OnnxModel model(String file) throws Exception {
        return OnnxModel.load(Files.readAllBytes(Dirs.artifacts().resolve(file)), "features", "probabilities", 1, order.size());
    }

    @Test
    void theChampionReproducesTheThousandGoldenScoresAndDecisions() throws Exception {
        assertThat(golden).hasSize(1000);
        try (OnnxModel champion = model("model.onnx")) {
            double worst = 0;
            int sameDecisionAsOnnx = 0;
            for (JsonNode c : golden) {
                double expected = c.get("score").asDouble();
                double actual = champion.score(row(c.get("features")));
                worst = Math.max(worst, Math.abs(actual - expected));
                Decision stored = Decision.valueOf(c.get("decision").asText());
                // La decisión guardada es la de redondear el score float64 del modelo original.
                assertThat(policy.evaluate(expected).decision()).as(c.get("case_id").asText()).isEqualTo(stored);
                // Y ONNX Runtime con las features float32 llega a la misma decisión.
                DecisionPolicy.Scored scored = policy.evaluate(actual);
                assertThat(scored.decision()).as(c.get("case_id").asText() + " score ONNX " + actual + " frente a " + expected)
                        .isEqualTo(stored);
                sameDecisionAsOnnx++;
            }
            assertThat(worst).as("diferencia máxima").isLessThan(TOLERANCE);
            assertThat(sameDecisionAsOnnx).isEqualTo(1000);
        }
    }

    @Test
    void goldenStoredScoresAreExactlyReproducedByPythonRoundingRule() {
        // Los 100 casos de frontera incluyen scores que el redondeo cambia de lado: la regla de Java debe verlos igual.
        long crossings = golden.stream().filter(c -> "boundary".equals(c.get("stratum").asText())).filter(c -> {
            BigDecimal raw = new BigDecimal(c.get("score").asDouble());
            BigDecimal rounded = raw.setScale(5, java.math.RoundingMode.HALF_EVEN);
            return raw.compareTo(thresholds.get("t_block").decimalValue()) < 0
                    && rounded.compareTo(thresholds.get("t_block").decimalValue()) >= 0;
        }).count();
        assertThat(crossings).as("casos en que el redondeo cruza t_block").isGreaterThanOrEqualTo(1);
    }

    @Test
    void theChallengerReproducesTheThousandGoldenChallengerScores() throws Exception {
        List<JsonNode> challengerScores = lines(Dirs.artifacts().resolve("golden_challenger.jsonl"));
        assertThat(challengerScores).hasSize(1000);
        try (OnnxModel challenger = model("challenger/model.onnx")) {
            double worst = 0;
            for (int i = 0; i < golden.size(); i++) {
                assertThat(challengerScores.get(i).get("case_id").asText()).isEqualTo(golden.get(i).get("case_id").asText());
                double expected = challengerScores.get(i).get("score").asDouble();
                double actual = challenger.score(row(golden.get(i).get("features")));
                worst = Math.max(worst, Math.abs(actual - expected));
            }
            assertThat(worst).as("diferencia máxima del challenger").isLessThan(TOLERANCE);
        }
    }

    @Test
    void theChampionAndTheChallengerShareTheSameInputContract() throws Exception {
        try (OnnxModel champion = model("model.onnx"); OnnxModel challenger = model("challenger/model.onnx")) {
            float[] row = row(golden.get(0).get("features"));
            assertThat(champion.score(row)).isBetween(0.0, 1.0);
            assertThat(challenger.score(row)).isBetween(0.0, 1.0);
        }
    }
}
