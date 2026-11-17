package com.verabank.risk.stream.aml;

import io.smallrye.mutiny.Uni;
import io.vertx.mutiny.sqlclient.Tuple;
import jakarta.enterprise.context.ApplicationScoped;
import java.util.List;

/** Origen en {@code risk.aml_exempt_accounts} (p. ej. CLEARING): ninguna regla posterior evalúa la transferencia. */
@ApplicationScoped
public class ExemptionRule implements AmlRule {

    public static final int ORDER = 10;

    @Override
    public int order() {
        return ORDER;
    }

    @Override
    public Uni<List<AlertCandidate>> handle(AmlContext context, Next next) {
        return context.db().preparedQuery("SELECT 1 FROM risk.aml_exempt_accounts WHERE account_id = $1")
                .execute(Tuple.of(context.fact().sourceAccountId()))
                .flatMap(rows -> rows.size() > 0 ? Uni.createFrom().item(List.<AlertCandidate>of()) : next.proceed());
    }
}
