package com.verabank.risk.stream.aml;

import io.smallrye.mutiny.Uni;
import java.util.ArrayList;
import java.util.List;

/** Regla que siempre pasa la transferencia al siguiente eslabón y suma lo suyo a lo que devuelva. */
public abstract class AccumulatingRule implements AmlRule {

    /** Alertas de esta regla para la transferencia (vacío si no dispara o no tiene versión activa). */
    protected abstract Uni<List<AlertCandidate>> evaluate(AmlContext context);

    @Override
    public final Uni<List<AlertCandidate>> handle(AmlContext context, Next next) {
        return evaluate(context).flatMap(own -> next.proceed().map(rest -> {
            List<AlertCandidate> all = new ArrayList<>(own);
            all.addAll(rest);
            return all;
        }));
    }
}
