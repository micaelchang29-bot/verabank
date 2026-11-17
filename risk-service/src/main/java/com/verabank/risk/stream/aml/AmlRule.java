package com.verabank.risk.stream.aml;

import io.smallrye.mutiny.Uni;
import java.util.List;

/**
 * Eslabón de la cadena de responsabilidad de las reglas AML (docs/diseno.md §2.5): cada regla decide si pasa la
 * transferencia al siguiente eslabón. La exención corta la cadena; las demás acumulan sus alertas.
 *
 * <p>Una regla nueva es un bean CDI más que implementa esta interfaz (normalmente extendiendo
 * {@link AccumulatingRule}): {@link AmlChain} las ordena por {@link #order()} y no se toca ninguna de las existentes.
 */
public interface AmlRule {

    /** Posición en la cadena; menor primero. */
    int order();

    Uni<List<AlertCandidate>> handle(AmlContext context, Next next);

    /** El resto de la cadena. */
    @FunctionalInterface
    interface Next {

        Uni<List<AlertCandidate>> proceed();
    }
}
