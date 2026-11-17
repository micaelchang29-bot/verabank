package com.verabank.risk.scoring;

import static com.verabank.risk.scoring.HandCases.T;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.within;

import com.verabank.risk.scoring.FeatureComputer.Windows;
import java.time.Instant;
import java.util.Map;
import org.junit.jupiter.api.Test;

/** Features con ejemplos calculados a mano ({@link HandCases}); no usa base de datos. */
class FeatureComputerTest {

    private final FeatureComputer computer = new FeatureComputer();

    private Map<String, Double> features(ScoreInput in, java.util.List<HandCases.Fact> facts) {
        Windows w = Windows.of(in.occurredAt());
        return computer.compute(in, InMemoryHistory.destination(facts, in, w), InMemoryHistory.source(facts, in, w));
    }

    @Test
    void windowsAreClosedOnTheLeftAndOpenOnTheRight() {
        Windows w = Windows.of(T);
        assertThat(w.to()).isEqualTo(T);
        assertThat(w.from1h()).isEqualTo(Instant.parse("2026-01-10T11:00:00Z"));
        assertThat(w.from24h()).isEqualTo(Instant.parse("2026-01-09T12:00:00Z"));
    }

    @Test
    void sourceWindowsIncludeTMinus1hAndTMinus24hAndExcludeTAndLater() {
        Map<String, Double> f = features(HandCases.request("src-A", "dst-N", "1000.00", "TRANSFER"), HandCases.sourceWindowFacts());
        assertThat(f.get("src_out_count_1h")).isEqualTo(1.0);          // solo t-1h
        assertThat(f.get("src_out_count_24h")).isEqualTo(3.0);         // t-1h, t-61min y t-24h
        assertThat(f.get("src_out_sum_24h")).isEqualTo(160.0);         // 100 + 50 + 10
    }

    @Test
    void newDestinationHasEveryHistoryFeatureAtItsFill() {
        Map<String, Double> f = features(HandCases.request("src-A", "dst-N", "1000.00", "TRANSFER"), HandCases.sourceWindowFacts());
        assertThat(f.get("dst_in_count_24h")).isZero();
        assertThat(f.get("dst_in_sum_24h")).isZero();
        assertThat(f.get("dst_distinct_src_24h")).isZero();
        assertThat(f.get("dst_seen_before")).isZero();
        assertThat(f.get("dst_age_hours")).isZero();
        assertThat(f.get("is_new_beneficiary")).isEqualTo(1.0);
    }

    @Test
    void repeatedDestinationCountsItsWindowAgeAndDistinctSources() {
        Map<String, Double> f = features(HandCases.request("src-G", "dst-D", "10.00", "TRANSFER"), HandCases.destinationFacts());
        assertThat(f.get("dst_in_count_24h")).isEqualTo(3.0);          // t-24h, t-3h, t-2h
        assertThat(f.get("dst_in_sum_24h")).isEqualTo(100.0);          // 40 + 30 + 30
        assertThat(f.get("dst_distinct_src_24h")).isEqualTo(2.0);      // src-E y src-C
        assertThat(f.get("dst_seen_before")).isEqualTo(1.0);
        assertThat(f.get("dst_age_hours")).isEqualTo(48.0);            // la primera aparición fue en t-48h
        assertThat(f.get("is_new_beneficiary")).isEqualTo(1.0);        // src-G nunca envió a dst-D
    }

    @Test
    void anExistingSourceDestinationPairIsNotANewBeneficiaryEvenOutsideTheWindow() {
        Map<String, Double> f = features(HandCases.request("src-B", "dst-D", "10.00", "TRANSFER"), HandCases.destinationFacts());
        assertThat(f.get("is_new_beneficiary")).isZero();              // src-B → dst-D fue en t-25h
        assertThat(f.get("dst_in_count_24h")).isEqualTo(3.0);          // y esa fila no está en la ventana
    }

    @Test
    void aRowAtTheSameInstantIsNotHistory() {
        Map<String, Double> f = features(HandCases.request("src-X", "dst-N", "10.00", "TRANSFER"), HandCases.onlySameInstantFacts());
        assertThat(f.get("dst_in_count_24h")).isZero();
        assertThat(f.get("dst_seen_before")).isZero();
        assertThat(f.get("dst_age_hours")).isZero();
        Map<String, Double> sameSource = features(HandCases.request("src-S", "dst-M", "10.00", "TRANSFER"), HandCases.onlySameInstantFacts());
        assertThat(sameSource.get("src_out_count_1h")).isZero();
        assertThat(sameSource.get("src_out_count_24h")).isZero();
    }

    @Test
    void requestFeaturesFollowTheContract() {
        Map<String, Double> f = features(HandCases.request("src-A", "dst-N", "1250.50", "CASH_OUT"), java.util.List.of());
        assertThat(f.get("amount")).isEqualTo(1250.5);
        assertThat(f.get("log_amount")).isCloseTo(Math.log1p(1250.5), within(1e-15));
        assertThat(f.get("type_TRANSFER")).isZero();
        assertThat(f.get("type_CASH_OUT")).isEqualTo(1.0);
        assertThat(f.get("type_PAYMENT")).isZero();
        assertThat(f.get("type_DEBIT")).isZero();
        // T es las 12:00 UTC: sin(π) ≈ 0 y cos(π) = -1.
        assertThat(f.get("hour_sin")).isCloseTo(0.0, within(1e-12));
        assertThat(f.get("hour_cos")).isCloseTo(-1.0, within(1e-12));
    }

    @Test
    void hourOfDayIsTheUtcHourAndFeaturesComeInContractOrder() {
        ScoreInput at18 = new ScoreInput(java.util.UUID.randomUUID(), "s", "d", new java.math.BigDecimal("1.00"), "PEN",
                "DEBIT", "PE", Instant.parse("2026-02-03T18:00:00Z"));
        Map<String, Double> f = computer.compute(at18, FeatureComputer.DestinationHistory.EMPTY, FeatureComputer.SourceHistory.EMPTY);
        assertThat(f.get("hour_sin")).isCloseTo(-1.0, within(1e-12));   // 18 h = 3π/2
        assertThat(f.get("hour_cos")).isCloseTo(0.0, within(1e-12));
        assertThat(f.keySet()).containsExactlyElementsOf(FeatureComputer.FEATURE_NAMES);
        assertThat(FeatureComputer.FEATURE_NAMES).hasSize(17);
    }

    @Test
    void ageIsAWholeNumberOfHoursByStepDifference() {
        assertThat(FeatureComputer.ageHours(Instant.parse("2026-01-01T00:00:00Z"), Instant.parse("2026-01-01T05:00:00Z"))).isEqualTo(5);
        assertThat(FeatureComputer.ageHours(Instant.parse("2026-01-01T00:59:59Z"), Instant.parse("2026-01-01T05:00:00Z"))).isEqualTo(5);
    }
}
