package com.verabank.risk.scoring;

import java.math.BigDecimal;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.UUID;

/**
 * Ejemplos calculados a mano para las features de historia, con el instante de la solicitud T = 2026-01-10T12:00:00Z.
 * Los usan la prueba pura de {@link FeatureComputer} (con {@link InMemoryHistory}) y la prueba contra PostgreSQL
 * real de las consultas: ambas deben dar lo mismo.
 */
final class HandCases {

    static final Instant T = Instant.parse("2026-01-10T12:00:00Z");

    record Fact(String source, String dest, String amount, Instant at) {
    }

    private HandCases() {
    }

    static Fact fact(String source, String dest, String amount, Duration before) {
        return new Fact(source, dest, amount, T.minus(before));
    }

    static Fact factAt(String source, String dest, String amount, Instant at) {
        return new Fact(source, dest, amount, at);
    }

    static ScoreInput request(String source, String dest, String amount, String type) {
        return new ScoreInput(UUID.randomUUID(), source, dest, new BigDecimal(amount), "PEN", type, "PE", T);
    }

    /**
     * Origen src-A: t-1h (100.00) cuenta en 1 h y 24 h; t-61min (50.00) y t-24h (10.00) solo en 24 h (cotas inferiores
     * cerradas); t-24h-1min (5.00), t (7.00, mismo instante) y t+1h (9.00, futura) no cuentan.
     * Esperado para src-A: 1 en la ventana de 1 h, 3 en la de 24 h, suma 160.00.
     */
    static List<Fact> sourceWindowFacts() {
        return List.of(
                fact("src-A", "dst-X", "100.00", Duration.ofHours(1)),
                fact("src-A", "dst-Y", "50.00", Duration.ofMinutes(61)),
                fact("src-A", "dst-Z", "10.00", Duration.ofHours(24)),
                fact("src-A", "dst-Z", "5.00", Duration.ofHours(24).plusMinutes(1)),
                factAt("src-A", "dst-W", "7.00", T),
                factAt("src-A", "dst-V", "9.00", T.plus(Duration.ofHours(1))));
    }

    /**
     * Destino dst-D: dentro de [t-24h, t): src-C 30.00 (t-3h), src-E 30.00 (t-2h) y src-E 40.00 (t-24h) = 3 filas, suma
     * 100.00, 2 orígenes distintos. Fuera: src-C 20.00 (t-48h, la primera aparición: antigüedad 48 h), src-B 5.00 (t-25h,
     * el par src-B → dst-D ya existía) y src-F 99.00 (mismo instante t, no cuenta ni como historia).
     */
    static List<Fact> destinationFacts() {
        return List.of(
                fact("src-C", "dst-D", "20.00", Duration.ofHours(48)),
                fact("src-B", "dst-D", "5.00", Duration.ofHours(25)),
                fact("src-E", "dst-D", "40.00", Duration.ofHours(24)),
                fact("src-C", "dst-D", "30.00", Duration.ofHours(3)),
                fact("src-E", "dst-D", "30.00", Duration.ofHours(2)),
                factAt("src-F", "dst-D", "99.00", T));
    }

    /** Una fila exactamente en t: no es historia, así que el destino sigue siendo nuevo. */
    static List<Fact> onlySameInstantFacts() {
        return List.of(factAt("src-S", "dst-N", "10.00", T));
    }
}
