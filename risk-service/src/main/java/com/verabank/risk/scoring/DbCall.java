package com.verabank.risk.scoring;

import com.verabank.risk.shared.ApiException;
import io.smallrye.mutiny.Uni;
import java.time.Duration;

/** Tiempo máximo por consulta (verabank.scoring.query-timeout, 100 ms por defecto). */
final class DbCall {

    private DbCall() {
    }

    static <T> Uni<T> limited(Uni<T> query, Duration timeout, String what) {
        return query.ifNoItem().after(timeout).failWith(() -> new ApiException(500, "internal-error", "Error interno",
                "La consulta '" + what + "' superó " + timeout.toMillis() + " ms"));
    }
}
