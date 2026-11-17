package com.verabank.risk.stream.aml;

import io.smallrye.mutiny.Uni;
import jakarta.enterprise.context.ApplicationScoped;
import java.util.List;

/** {@code THRESHOLD}: una transferencia con monto &ge; umbral (10.000,00) es una alerta {@code MEDIUM}. */
@ApplicationScoped
public class ThresholdRule extends AccumulatingRule {

    public static final String CODE = "THRESHOLD";

    @Override
    public int order() {
        return 20;
    }

    @Override
    protected Uni<List<AlertCandidate>> evaluate(AmlContext context) {
        return Uni.createFrom().item(() -> {
            var params = context.params(CODE);
            TransferFact fact = context.fact();
            if (params.isEmpty() || fact.amount().compareTo(params.get().decimal("threshold")) < 0) {
                return List.of();
            }
            RuleParams p = params.get();
            return List.of(new AlertCandidate(CODE, p.version(), p.json(), "MEDIUM",
                    CODE + "|" + fact.sourceAccountId() + "|" + fact.transferId(), fact.sourceAccountId(),
                    fact.occurredAt(), fact.occurredAt(), fact.amount(), List.of(fact.transferId())));
        });
    }
}
