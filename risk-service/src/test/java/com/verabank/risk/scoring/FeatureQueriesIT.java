package com.verabank.risk.scoring;

import static org.assertj.core.api.Assertions.assertThat;

import com.verabank.risk.scoring.FeatureComputer.DestinationHistory;
import com.verabank.risk.scoring.FeatureComputer.SourceHistory;
import com.verabank.risk.scoring.FeatureComputer.Windows;
import com.verabank.risk.support.Db;
import com.verabank.risk.support.PgInfra;
import io.quarkus.test.junit.QuarkusTest;
import jakarta.inject.Inject;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/**
 * Las dos consultas SQL dan lo mismo que la referencia en memoria con los ejemplos a mano (límites t-1h, t-24h, mismo
 * instante, destino nuevo y repetido, orígenes distintos). Las filas {@code warmup} cuentan como historia (D1).
 */
@QuarkusTest
class FeatureQueriesIT {

    @Inject
    FeatureQueries queries;

    @BeforeEach
    void clean() {
        PgInfra.truncateLog();
    }

    private void load(List<HandCases.Fact> facts) {
        facts.forEach(f -> Db.insertHistory(f.source(), f.dest(), f.amount(), f.at(), "warmup"));
    }

    private void assertSqlEqualsReference(List<HandCases.Fact> facts, ScoreInput in) {
        Windows w = Windows.of(in.occurredAt());
        DestinationHistory dest = queries.destination(in, w).await().indefinitely();
        SourceHistory source = queries.source(in, w).await().indefinitely();
        DestinationHistory expectedDest = InMemoryHistory.destination(facts, in, w);
        SourceHistory expectedSource = InMemoryHistory.source(facts, in, w);
        assertThat(dest.count24h()).isEqualTo(expectedDest.count24h());
        assertThat(dest.sum24h()).isEqualByComparingTo(expectedDest.sum24h());
        assertThat(dest.distinctSources24h()).isEqualTo(expectedDest.distinctSources24h());
        assertThat(dest.firstSeen()).isEqualTo(expectedDest.firstSeen());
        assertThat(dest.pairSeen()).isEqualTo(expectedDest.pairSeen());
        assertThat(source.count1h()).isEqualTo(expectedSource.count1h());
        assertThat(source.count24h()).isEqualTo(expectedSource.count24h());
        assertThat(source.sum24h()).isEqualByComparingTo(expectedSource.sum24h());
    }

    @Test
    void sourceWindowBoundariesMatchTheHandExample() {
        List<HandCases.Fact> facts = HandCases.sourceWindowFacts();
        load(facts);
        ScoreInput in = HandCases.request("src-A", "dst-N", "1000.00", "TRANSFER");
        assertSqlEqualsReference(facts, in);
        SourceHistory source = queries.source(in, Windows.of(in.occurredAt())).await().indefinitely();
        assertThat(source.count1h()).isEqualTo(1);
        assertThat(source.count24h()).isEqualTo(3);
        assertThat(source.sum24h()).isEqualByComparingTo("160.00");
    }

    @Test
    void destinationHistoryMatchesTheHandExample() {
        List<HandCases.Fact> facts = HandCases.destinationFacts();
        load(facts);
        ScoreInput stranger = HandCases.request("src-G", "dst-D", "10.00", "TRANSFER");
        assertSqlEqualsReference(facts, stranger);
        DestinationHistory d = queries.destination(stranger, Windows.of(HandCases.T)).await().indefinitely();
        assertThat(d.count24h()).isEqualTo(3);
        assertThat(d.sum24h()).isEqualByComparingTo("100.00");
        assertThat(d.distinctSources24h()).isEqualTo(2);
        assertThat(d.firstSeen()).isEqualTo(HandCases.T.minus(java.time.Duration.ofHours(48)));
        assertThat(d.pairSeen()).isFalse();
        // src-B ya había enviado a dst-D (fuera de la ventana de 24 h): el par existe.
        ScoreInput known = HandCases.request("src-B", "dst-D", "10.00", "TRANSFER");
        assertSqlEqualsReference(facts, known);
        assertThat(queries.destination(known, Windows.of(HandCases.T)).await().indefinitely().pairSeen()).isTrue();
    }

    @Test
    void aRowAtTheSameInstantIsNeitherInTheWindowNorHistory() {
        List<HandCases.Fact> facts = HandCases.onlySameInstantFacts();
        load(facts);
        ScoreInput in = HandCases.request("src-S", "dst-N", "10.00", "TRANSFER");
        assertSqlEqualsReference(facts, in);
        DestinationHistory d = queries.destination(in, Windows.of(in.occurredAt())).await().indefinitely();
        assertThat(d.count24h()).isZero();
        assertThat(d.firstSeen()).isNull();
        SourceHistory s = queries.source(in, Windows.of(in.occurredAt())).await().indefinitely();
        assertThat(s.count1h()).isZero();
        assertThat(s.count24h()).isZero();
    }

    @Test
    void warmupRowsCountAsHistory() {
        Db.insertHistory("src-W", "dst-W", "10.00", HandCases.T.minus(java.time.Duration.ofHours(2)), "warmup");
        ScoreInput in = HandCases.request("src-W", "dst-W", "10.00", "TRANSFER");
        assertThat(queries.destination(in, Windows.of(in.occurredAt())).await().indefinitely().count24h()).isEqualTo(1);
        assertThat(queries.source(in, Windows.of(in.occurredAt())).await().indefinitely().count1h()).isZero();
        assertThat(queries.source(in, Windows.of(in.occurredAt())).await().indefinitely().count24h()).isEqualTo(1);
    }
}
