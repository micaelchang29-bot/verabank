package com.verabank.risk.stream.aml;

import io.smallrye.mutiny.Uni;
import io.vertx.mutiny.sqlclient.Tuple;
import io.vertx.sqlclient.data.Numeric;
import jakarta.enterprise.context.ApplicationScoped;
import java.math.BigDecimal;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

/**
 * {@code STRUCTURING} (fraccionamiento): transferencias del mismo origen en {@code [t − window_hours, t]}, cada una
 * con {@code min_ratio ≤ monto / umbral < max_ratio} (el máximo es exclusivo). Con {@code min_count} o más y suma
 * &ge; {@code min_total}, una alerta {@code HIGH} cuya clave es el inicio del conjunto: una transferencia nueva del
 * mismo conjunto actualiza la misma alerta en vez de crear otra.
 */
@ApplicationScoped
public class StructuringRule extends AccumulatingRule {

    public static final String CODE = "STRUCTURING";

    private static final String WINDOW = """
            SELECT transfer_id, amount, occurred_at FROM risk.transfer_facts
             WHERE source_account_id = $1 AND occurred_at >= $2 AND occurred_at <= $3
               AND amount >= $4 AND amount < $5
             ORDER BY occurred_at, transfer_id
            """;

    @Override
    public int order() {
        return 30;
    }

    @Override
    protected Uni<List<AlertCandidate>> evaluate(AmlContext context) {
        var params = context.params(CODE);
        if (params.isEmpty()) {
            return Uni.createFrom().item(List.of());
        }
        RuleParams p = params.get();
        TransferFact fact = context.fact();
        BigDecimal threshold = p.decimal("threshold");
        BigDecimal low = threshold.multiply(p.decimal("min_ratio"));
        BigDecimal high = threshold.multiply(p.decimal("max_ratio"));
        if (fact.amount().compareTo(low) < 0 || fact.amount().compareTo(high) >= 0) {
            return Uni.createFrom().item(List.of());   // esta transferencia no está en la banda: el conjunto no cambia
        }
        Instant from = fact.occurredAt().minus(Duration.ofHours(p.integer("window_hours")));
        Tuple args = Tuple.tuple().addValue(fact.sourceAccountId())
                .addValue(from.atOffset(ZoneOffset.UTC)).addValue(fact.occurredAt().atOffset(ZoneOffset.UTC))
                .addValue(Numeric.create(low)).addValue(Numeric.create(high));
        return context.db().preparedQuery(WINDOW).execute(args).map(rows -> {
            List<UUID> ids = new ArrayList<>();
            BigDecimal total = BigDecimal.ZERO;
            Instant first = null;
            for (var row : rows) {
                ids.add(row.getUUID("transfer_id"));
                total = total.add(row.getNumeric("amount").bigDecimalValue());
                if (first == null) {
                    first = row.getOffsetDateTime("occurred_at").toInstant();
                }
            }
            if (ids.size() < p.integer("min_count") || total.compareTo(p.decimal("min_total")) < 0) {
                return List.<AlertCandidate>of();
            }
            return List.of(new AlertCandidate(CODE, p.version(), p.json(), "HIGH",
                    CODE + "|" + fact.sourceAccountId() + "|" + first, fact.sourceAccountId(), first,
                    fact.occurredAt(), total, ids));
        });
    }
}
