package com.verabank.risk.stream.aml;

import io.smallrye.mutiny.Uni;
import io.vertx.mutiny.sqlclient.Tuple;
import jakarta.enterprise.context.ApplicationScoped;
import java.math.BigDecimal;
import java.time.LocalDate;
import java.time.ZoneOffset;
import java.util.List;

/**
 * {@code COUNTRY_RISK}: país del destino con nivel vigente en {@code occurred_at} en {@code risk.country_risk}.
 * {@code HIGH} con monto &ge; {@code high_min_amount} es {@code HIGH}; {@code WATCH} con monto &ge;
 * {@code watch_min_amount} es {@code MEDIUM}.
 */
@ApplicationScoped
public class CountryRiskRule extends AccumulatingRule {

    public static final String CODE = "COUNTRY_RISK";

    private static final String LEVEL = """
            SELECT level FROM risk.country_risk
             WHERE country = $1 AND valid_from <= $2 AND (valid_to IS NULL OR $2 < valid_to)
            """;

    @Override
    public int order() {
        return 40;
    }

    @Override
    protected Uni<List<AlertCandidate>> evaluate(AmlContext context) {
        var params = context.params(CODE);
        if (params.isEmpty()) {
            return Uni.createFrom().item(List.of());
        }
        RuleParams p = params.get();
        TransferFact fact = context.fact();
        LocalDate day = LocalDate.ofInstant(fact.occurredAt(), ZoneOffset.UTC);
        return context.db().preparedQuery(LEVEL).execute(Tuple.of(fact.destCountry(), day)).map(rows -> {
            if (rows.size() == 0) {
                return List.<AlertCandidate>of();
            }
            String level = rows.iterator().next().getString("level");
            String severity = null;
            BigDecimal amount = fact.amount();
            if ("HIGH".equals(level) && amount.compareTo(p.decimal("high_min_amount")) >= 0) {
                severity = "HIGH";
            } else if ("WATCH".equals(level) && amount.compareTo(p.decimal("watch_min_amount")) >= 0) {
                severity = "MEDIUM";
            }
            if (severity == null) {
                return List.<AlertCandidate>of();
            }
            return List.of(new AlertCandidate(CODE, p.version(), p.json(), severity,
                    CODE + "|" + fact.sourceAccountId() + "|" + fact.transferId(), fact.sourceAccountId(),
                    fact.occurredAt(), fact.occurredAt(), amount, List.of(fact.transferId())));
        });
    }
}
