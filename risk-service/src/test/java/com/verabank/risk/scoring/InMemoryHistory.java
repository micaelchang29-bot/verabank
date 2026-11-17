package com.verabank.risk.scoring;

import com.verabank.risk.scoring.FeatureComputer.DestinationHistory;
import com.verabank.risk.scoring.FeatureComputer.SourceHistory;
import com.verabank.risk.scoring.FeatureComputer.Windows;
import com.verabank.risk.scoring.HandCases.Fact;
import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;

/** Referencia en memoria de las dos consultas: las mismas ventanas {@code [t - w, t)} sobre una lista de filas. */
final class InMemoryHistory {

    private InMemoryHistory() {
    }

    static DestinationHistory destination(List<Fact> facts, ScoreInput in, Windows w) {
        List<Fact> toDest = facts.stream().filter(f -> f.dest().equals(in.destRef())).toList();
        List<Fact> window = toDest.stream().filter(f -> inWindow(f.at(), w.from24h(), w.to())).toList();
        Instant first = toDest.stream().map(Fact::at).filter(at -> at.isBefore(w.to())).min(Instant::compareTo).orElse(null);
        boolean pair = toDest.stream().anyMatch(f -> f.source().equals(in.sourceRef()) && f.at().isBefore(w.to()));
        return new DestinationHistory(window.size(), sum(window), window.stream().map(Fact::source).distinct().count(), first, pair);
    }

    static SourceHistory source(List<Fact> facts, ScoreInput in, Windows w) {
        List<Fact> own = facts.stream().filter(f -> f.source().equals(in.sourceRef())).toList();
        List<Fact> day = own.stream().filter(f -> inWindow(f.at(), w.from24h(), w.to())).toList();
        long hour = own.stream().filter(f -> inWindow(f.at(), w.from1h(), w.to())).count();
        return new SourceHistory(hour, day.size(), sum(day));
    }

    private static boolean inWindow(Instant at, Instant from, Instant to) {
        return !at.isBefore(from) && at.isBefore(to);
    }

    private static BigDecimal sum(List<Fact> facts) {
        return facts.stream().map(f -> new BigDecimal(f.amount())).reduce(BigDecimal.ZERO, BigDecimal::add);
    }
}
