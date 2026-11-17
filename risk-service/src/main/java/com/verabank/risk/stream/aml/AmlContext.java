package com.verabank.risk.stream.aml;

import io.vertx.mutiny.sqlclient.SqlClient;
import java.util.Optional;

/** Lo que ve cada regla: la conexión de la transacción en curso, la transferencia y los parámetros vigentes. */
public record AmlContext(SqlClient db, TransferFact fact, RuleCatalog.Snapshot rules) {

    /** Parámetros de la versión activa; vacío si la regla no tiene versión activa (queda desactivada). */
    public Optional<RuleParams> params(String ruleCode) {
        return rules.get(ruleCode);
    }
}
