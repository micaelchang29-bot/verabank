package com.verabank.risk.stream;

import com.verabank.risk.stream.aml.AccumulatingRule;
import com.verabank.risk.stream.aml.AlertCandidate;
import com.verabank.risk.stream.aml.AmlContext;
import io.smallrye.mutiny.Uni;
import jakarta.enterprise.context.ApplicationScoped;
import java.math.BigDecimal;
import java.util.List;

/**
 * Regla ficticia que se añade a la cadena SIN tocar ninguna de las existentes (modelo de la futura FAN_IN): solo
 * dispara si una prueba la activa y la transferencia es de 777,77.
 */
@ApplicationScoped
public class FictitiousRule extends AccumulatingRule {

    public static volatile boolean enabled;

    @Override
    public int order() {
        return 50;   // después de COUNTRY_RISK
    }

    @Override
    protected Uni<List<AlertCandidate>> evaluate(AmlContext context) {
        var fact = context.fact();
        if (!enabled || fact.amount().compareTo(new BigDecimal("777.77")) != 0) {
            return Uni.createFrom().item(List.of());
        }
        return Uni.createFrom().item(List.of(new AlertCandidate("FICTITIOUS", 1, "{}", "LOW",
                "FICTITIOUS|" + fact.sourceAccountId() + "|" + fact.transferId(), fact.sourceAccountId(),
                fact.occurredAt(), fact.occurredAt(), fact.amount(), List.of(fact.transferId()))));
    }
}
