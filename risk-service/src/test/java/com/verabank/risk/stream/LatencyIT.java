package com.verabank.risk.stream;

import static com.verabank.risk.stream.StreamSupport.T0;
import static org.assertj.core.api.Assertions.assertThat;

import com.verabank.risk.support.Db;
import com.verabank.risk.support.KafkaInfra;
import io.quarkus.test.junit.QuarkusTest;
import io.quarkus.test.junit.TestProfile;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import org.awaitility.Awaitility;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/**
 * Latencia evento → alerta persistida sobre 200 eventos (objetivo de §1.2: p95 &lt; 5 s). Se mide de dos formas: la
 * columna {@code created_at − trigger_emitted_at} (la que explota KPI 3) y la observada desde fuera, sondeando la
 * tabla cada 5 ms (incluye el commit). El informe queda en target/latency-report.txt.
 */
@QuarkusTest
@TestProfile(StreamProfile.class)
class LatencyIT {

    private static final int EVENTS = 200;
    private static final Duration OBJECTIVE = Duration.ofSeconds(5);

    @BeforeEach
    void reset() {
        StreamDb.reset();
    }

    private record Result(String name, double columnP50, double columnP95, double observedP50, double observedP95,
            double observedMax) {
    }

    @Test
    void pacedAt20EventsPerSecond() throws Exception {
        Result r = measure("ritmo sostenido (20 eventos/s)", Duration.ofMillis(50));
        assertThat(r.observedP95()).as("p95 observado (ms)").isLessThan(OBJECTIVE.toMillis());
        assertThat(r.columnP95()).as("p95 created_at - trigger_emitted_at (ms)").isLessThan(OBJECTIVE.toMillis());
    }

    @Test
    void burstOfAllEventsAtOnce() throws Exception {
        Result r = measure("ráfaga (200 eventos de golpe)", Duration.ZERO);
        assertThat(r.observedP95()).as("p95 observado (ms)").isLessThan(OBJECTIVE.toMillis());
    }

    private Result measure(String name, Duration spacing) throws Exception {
        Map<UUID, Instant> emitted = new HashMap<>();
        Map<UUID, Instant> seen = new HashMap<>();
        List<UUID> sources = new ArrayList<>();
        Thread poller = new Thread(() -> poll(seen), "latency-poller");
        poller.setDaemon(true);
        poller.start();
        try {
            for (int i = 0; i < EVENTS; i++) {
                UUID source = UUID.randomUUID();
                sources.add(source);
                var event = Events.transfer(source, "10000.00", T0.plusSeconds(i));
                event.emittedAt = Instant.now();
                emitted.put(source, event.emittedAt);
                // Asíncrono respecto al broker: send() espera la confirmación, así que la ráfaga es todo lo rápida que el productor deja.
                KafkaInfra.send(KafkaInfra.TRANSFER_EVENTS, source.toString(), event.json());
                if (!spacing.isZero()) {
                    Thread.sleep(spacing.toMillis());
                }
            }
            Awaitility.await().atMost(60, TimeUnit.SECONDS).pollInterval(Duration.ofMillis(100))
                    .until(() -> Db.queryLong("SELECT count(*) FROM risk.aml_alerts") == EVENTS);
            Awaitility.await().atMost(5, TimeUnit.SECONDS).until(() -> {
                synchronized (seen) {
                    return seen.size() == EVENTS;
                }
            });
        } finally {
            poller.interrupt();
        }
        List<Double> column = new ArrayList<>();
        try (Connection c = com.verabank.risk.support.PgInfra.admin(); Statement s = c.createStatement();
                ResultSet rs = s.executeQuery("SELECT EXTRACT(EPOCH FROM (created_at - trigger_emitted_at)) * 1000 FROM risk.aml_alerts")) {
            while (rs.next()) {
                column.add(rs.getDouble(1));
            }
        }
        List<Double> observed = new ArrayList<>();
        synchronized (seen) {
            for (UUID source : sources) {
                observed.add((double) Duration.between(emitted.get(source), seen.get(source)).toMillis());
            }
        }
        Result result = new Result(name, percentile(column, 50), percentile(column, 95), percentile(observed, 50),
                percentile(observed, 95), observed.stream().mapToDouble(d -> d).max().orElse(0));
        report(result);
        return result;
    }

    private static void poll(Map<UUID, Instant> seen) {
        try (Connection c = com.verabank.risk.support.PgInfra.admin();
                Statement s = c.createStatement()) {
            while (!Thread.currentThread().isInterrupted()) {
                try (ResultSet rs = s.executeQuery("SELECT subject_account_id FROM risk.aml_alerts")) {
                    Instant now = Instant.now();
                    synchronized (seen) {
                        while (rs.next()) {
                            seen.putIfAbsent(rs.getObject(1, UUID.class), now);
                        }
                    }
                }
                Thread.sleep(5);
            }
        } catch (SQLException e) {
            throw new IllegalStateException(e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    static double percentile(List<Double> values, int p) {
        List<Double> sorted = values.stream().sorted().toList();
        int index = (int) Math.ceil(p / 100.0 * sorted.size()) - 1;
        return sorted.get(Math.max(0, Math.min(index, sorted.size() - 1)));
    }

    private static void report(Result r) throws IOException {
        String line = String.format("%s | %d eventos | created_at-trigger_emitted_at: p50=%.0f ms p95=%.0f ms | "
                + "observado: p50=%.0f ms p95=%.0f ms máx=%.0f ms | objetivo p95 < %d ms%n", r.name, EVENTS,
                r.columnP50, r.columnP95, r.observedP50, r.observedP95, r.observedMax, OBJECTIVE.toMillis());
        System.out.print("LATENCIA " + line);
        Path out = Path.of("target", "latency-report.txt");
        Files.writeString(out, line, java.nio.file.StandardOpenOption.CREATE, java.nio.file.StandardOpenOption.APPEND);
    }
}
