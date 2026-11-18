package com.verabank.risk.scoring;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.verabank.risk.support.Dirs;
import java.math.BigDecimal;
import java.nio.file.Files;
import java.util.List;
import org.junit.jupiter.api.Test;

/** T-11: límites inclusivos de t_review y t_block con el redondeo a 5 decimales de ml/README.md. */
class DecisionPolicyTest {

    private static final BigDecimal STEP = new BigDecimal("0.00001");

    record Pair(String name, BigDecimal review, BigDecimal block) {
    }

    private static List<Pair> pairs() throws Exception {
        JsonNode t = new ObjectMapper().enable(DeserializationFeature.USE_BIG_DECIMAL_FOR_FLOATS)
                .readTree(Files.readAllBytes(Dirs.artifacts().resolve("thresholds.json")));
        return List.of(
                new Pair("reales", t.get("t_review").decimalValue(), t.get("t_block").decimalValue()),
                new Pair("sintéticos medios", new BigDecimal("0.25000"), new BigDecimal("0.75000")),
                new Pair("sintéticos cerca de 0 y 1", new BigDecimal("0.00005"), new BigDecimal("0.99999")),
                new Pair("sintéticos con 5.º decimal impar", new BigDecimal("0.12345"), new BigDecimal("0.54321")));
    }

    private static DecisionPolicy policy(Pair p) {
        return new ThresholdDecisionPolicy(p.review(), p.block(), 5);
    }

    private static Decision decide(Pair p, BigDecimal score) {
        return policy(p).evaluate(score.doubleValue()).decision();
    }

    @Test
    void reviewBoundaryIsInclusiveAndOneStepBelowIsApprove() throws Exception {
        for (Pair p : pairs()) {
            assertThat(decide(p, p.review().subtract(STEP))).as(p.name()).isEqualTo(Decision.APPROVE);
            assertThat(decide(p, p.review())).as(p.name()).isEqualTo(Decision.REVIEW);
            assertThat(decide(p, p.review().add(STEP))).as(p.name()).isEqualTo(Decision.REVIEW);
        }
    }

    @Test
    void blockBoundaryIsInclusiveAndOneStepBelowIsReview() throws Exception {
        for (Pair p : pairs()) {
            assertThat(decide(p, p.block().subtract(STEP))).as(p.name()).isEqualTo(Decision.REVIEW);
            assertThat(decide(p, p.block())).as(p.name()).isEqualTo(Decision.BLOCK);
            if (p.block().add(STEP).compareTo(BigDecimal.ONE) <= 0) {
                assertThat(decide(p, p.block().add(STEP))).as(p.name()).isEqualTo(Decision.BLOCK);
            }
        }
    }

    @Test
    void scoresThatRoundOntoAThresholdTakeTheUpperDecision() throws Exception {
        for (Pair p : pairs()) {
            for (BigDecimal t : List.of(p.review(), p.block())) {
                BigDecimal justBelow = t.subtract(new BigDecimal("0.000004"));   // redondea hasta t
                BigDecimal clearlyBelow = t.subtract(new BigDecimal("0.000006")); // redondea a t - 0,00001
                Decision upper = t.equals(p.review()) ? Decision.REVIEW : Decision.BLOCK;
                Decision lower = t.equals(p.review()) ? Decision.APPROVE : Decision.REVIEW;
                if (justBelow.signum() >= 0) {
                    assertThat(decide(p, justBelow)).as(p.name() + " " + justBelow).isEqualTo(upper);
                }
                if (clearlyBelow.signum() >= 0) {
                    assertThat(decide(p, clearlyBelow)).as(p.name() + " " + clearlyBelow).isEqualTo(lower);
                }
            }
        }
    }

    /**
     * Umbrales escritos a mano, a propósito, y no leídos de thresholds.json: la duplicación es deliberada. Los demás
     * casos de esta clase toman los umbrales del mismo archivo que el servicio, así que un valor alterado allí (con su
     * suma de verificación al día en manifest.json) los movería a la vez. Estos valores son los de thresholds.json,
     * ml/reports/valid_metrics.json y ml/reports/test_evaluation.json (t_review 0,83142; t_block 0,9038). Si se
     * reentrena y cambian, hay que actualizarlos aquí a propósito.
     */
    @Test
    void literalThresholdsGiveInclusiveBoundariesAndMatchThresholdsJson() throws Exception {
        BigDecimal review = new BigDecimal("0.83142");
        BigDecimal block = new BigDecimal("0.9038");
        Pair literal = new Pair("literales", review, block);
        assertThat(pairs().get(0).review()).as("t_review de thresholds.json").isEqualByComparingTo(review);
        assertThat(pairs().get(0).block()).as("t_block de thresholds.json").isEqualByComparingTo(block);
        assertThat(decide(literal, new BigDecimal("0.83141"))).isEqualTo(Decision.APPROVE);
        assertThat(decide(literal, review)).isEqualTo(Decision.REVIEW);
        assertThat(decide(literal, new BigDecimal("0.83143"))).isEqualTo(Decision.REVIEW);
        assertThat(decide(literal, new BigDecimal("0.90379"))).isEqualTo(Decision.REVIEW);
        assertThat(decide(literal, block)).isEqualTo(Decision.BLOCK);
        assertThat(decide(literal, new BigDecimal("0.90381"))).isEqualTo(Decision.BLOCK);
    }

    @Test
    void ties_round_half_even_on_the_exact_double_like_python() {
        // 0.015625 = 1/64 es exacto en binario: el 6.º decimal es 5 y no hay más. Python: round(0.015625, 5) = 0.01562.
        ThresholdDecisionPolicy policy = new ThresholdDecisionPolicy(new BigDecimal("0.01563"), new BigDecimal("0.5"), 5);
        DecisionPolicy.Scored tie = policy.evaluate(0.015625);
        assertThat(tie.score()).isEqualByComparingTo("0.01562");
        assertThat(tie.decision()).isEqualTo(Decision.APPROVE);          // half-up daría 0.01563 → REVIEW
        // 3/64 = 0.046875 → half-even 0.04688 (el 7 sube al par 8).
        assertThat(policy.evaluate(0.046875).score()).isEqualByComparingTo("0.04688");
        // Otro empate hacia abajo: 5/64 = 0.078125 → 0.07812.
        assertThat(policy.evaluate(0.078125).score()).isEqualByComparingTo("0.07812");
    }

    @Test
    void roundingUsesTheExactBinaryValueNotItsShortestDecimalText() {
        // 0.125 + 2^-30 está algo por encima de la mitad en el sexto decimal: Double.toString lo muestra corto, el
        // valor exacto no es un empate.
        double aboveTie = Math.nextUp(0.015625);
        assertThat(new ThresholdDecisionPolicy(new BigDecimal("0.01563"), new BigDecimal("0.5"), 5)
                .evaluate(aboveTie).score()).isEqualByComparingTo("0.01563");
    }

    @Test
    void scoreHasFiveDecimalsAndInvalidProbabilitiesAreRejected() {
        ThresholdDecisionPolicy policy = new ThresholdDecisionPolicy(new BigDecimal("0.5"), new BigDecimal("0.9"), 5);
        assertThat(policy.evaluate(0.5).score().scale()).isEqualTo(5);
        assertThat(policy.evaluate(0.0).score().toPlainString()).isEqualTo("0.00000");
        assertThatThrownBy(() -> policy.evaluate(Double.NaN)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> policy.evaluate(1.0000001)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> policy.evaluate(-0.1)).isInstanceOf(IllegalArgumentException.class);
    }
}
