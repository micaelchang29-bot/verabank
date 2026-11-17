package com.verabank.risk.stream.aml;

import io.quarkus.arc.All;
import io.smallrye.mutiny.Uni;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import java.util.Comparator;
import java.util.List;

/** Ordena las {@link AmlRule} por {@link AmlRule#order()} y las encadena. */
@ApplicationScoped
public class AmlChain {

    private final List<AmlRule> rules;

    @Inject
    public AmlChain(@All List<AmlRule> rules) {
        this.rules = rules.stream().sorted(Comparator.comparingInt(AmlRule::order)).toList();
    }

    public List<AmlRule> rules() {
        return rules;
    }

    public Uni<List<AlertCandidate>> run(AmlContext context) {
        return step(0, context);
    }

    private Uni<List<AlertCandidate>> step(int index, AmlContext context) {
        if (index >= rules.size()) {
            return Uni.createFrom().item(List.of());
        }
        return rules.get(index).handle(context, () -> step(index + 1, context));
    }
}
