package com.verabank.risk.scoring;

import java.math.BigDecimal;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.temporal.ChronoUnit;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Las 17 features del conjunto A de contracts/features.yaml, calculadas en {@code double} (la conversión a
 * float32 ocurre solo al entrar a ONNX Runtime). Es una clase pura: no toca la base. Definiciones, rellenos y
 * nombres: ml/README.md, secciones «Features» y «Contrato con el servicio de scoring».
 *
 * <p>Las ventanas son {@code [t - w, t)} sobre {@code occurred_at}: una fila con {@code occurred_at >= t} nunca
 * cuenta, tampoco la de la misma hora. {@link Windows} fija esos límites y los usan las consultas SQL.
 */
public final class FeatureComputer {

    /** Orden del contrato (features.yaml; type se expande a type_<categoría>). */
    public static final List<String> FEATURE_NAMES = List.of(
            "amount", "log_amount", "type_TRANSFER", "type_CASH_OUT", "type_PAYMENT", "type_DEBIT",
            "hour_sin", "hour_cos", "dst_in_count_24h", "dst_in_sum_24h", "dst_distinct_src_24h",
            "dst_seen_before", "dst_age_hours", "src_out_count_1h", "src_out_count_24h", "src_out_sum_24h",
            "is_new_beneficiary");

    public static final List<String> TYPES = List.of("TRANSFER", "CASH_OUT", "PAYMENT", "DEBIT");

    /** Límites de las ventanas de una solicitud en {@code t}: 1 h = [from1h, t); 24 h = [from24h, t). */
    public record Windows(Instant to, Instant from24h, Instant from1h) {

        public static Windows of(Instant t) {
            return new Windows(t, t.minus(24, ChronoUnit.HOURS), t.minus(1, ChronoUnit.HOURS));
        }
    }

    /** Lo que la consulta por destino devuelve: ventana de 24 h, primera aparición y par origen-destino previo. */
    public record DestinationHistory(
            long count24h, BigDecimal sum24h, long distinctSources24h, Instant firstSeen, boolean pairSeen) {

        public static final DestinationHistory EMPTY = new DestinationHistory(0, BigDecimal.ZERO, 0, null, false);
    }

    /** Lo que la consulta por origen devuelve: intentos previos, incluidos los rechazados (ADR-07). */
    public record SourceHistory(long count1h, long count24h, BigDecimal sum24h) {

        public static final SourceHistory EMPTY = new SourceHistory(0, 0, BigDecimal.ZERO);
    }

    public Map<String, Double> compute(ScoreInput in, DestinationHistory dest, SourceHistory source) {
        double amount = in.amount().doubleValue();
        int hour = in.occurredAt().atOffset(ZoneOffset.UTC).getHour();
        boolean seen = dest.firstSeen() != null;
        Map<String, Double> f = new LinkedHashMap<>();
        f.put("amount", amount);
        f.put("log_amount", Math.log1p(amount));
        for (String type : TYPES) {
            f.put("type_" + type, type.equals(in.transferType()) ? 1.0 : 0.0);
        }
        f.put("hour_sin", Math.sin(2 * Math.PI * hour / 24));
        f.put("hour_cos", Math.cos(2 * Math.PI * hour / 24));
        f.put("dst_in_count_24h", (double) dest.count24h());
        f.put("dst_in_sum_24h", dest.sum24h().doubleValue());
        f.put("dst_distinct_src_24h", (double) dest.distinctSources24h());
        f.put("dst_seen_before", seen ? 1.0 : 0.0);
        // Diferencia de pasos (horas enteras) con la primera aparición del destino; 0 si no se ha visto.
        f.put("dst_age_hours", seen ? (double) ageHours(dest.firstSeen(), in.occurredAt()) : 0.0);
        f.put("src_out_count_1h", (double) source.count1h());
        f.put("src_out_count_24h", (double) source.count24h());
        f.put("src_out_sum_24h", source.sum24h().doubleValue());
        f.put("is_new_beneficiary", dest.pairSeen() ? 0.0 : 1.0);
        return f;
    }

    static long ageHours(Instant firstSeen, Instant t) {
        return ChronoUnit.HOURS.between(firstSeen.truncatedTo(ChronoUnit.HOURS), t.truncatedTo(ChronoUnit.HOURS));
    }

    /** Fila float32 en el orden dado (el de thresholds.json:feature_order). */
    public static float[] row(Map<String, Double> features, List<String> order) {
        float[] row = new float[order.size()];
        for (int i = 0; i < row.length; i++) {
            row[i] = features.get(order.get(i)).floatValue();
        }
        return row;
    }
}
