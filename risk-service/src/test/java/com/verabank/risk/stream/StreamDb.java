package com.verabank.risk.stream;

import com.verabank.risk.support.PgInfra;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.UUID;

/** Preparación del estado de risk para las pruebas del rol stream (como administrador). */
public final class StreamDb {

    private StreamDb() {
    }

    /** Vacía lo que escribe el rol stream y las listas de apoyo; deja las reglas v1 activas. */
    public static void reset() {
        try (Connection c = PgInfra.admin(); Statement s = c.createStatement()) {
            s.execute("TRUNCATE risk.transfer_facts, risk.aml_alerts, risk.processed_messages, risk.fraud_labels, "
                    + "risk.country_risk, risk.aml_exempt_accounts");
            s.execute("DELETE FROM risk.aml_rules WHERE version > 1");
            s.execute("UPDATE risk.aml_rules SET active = (version = 1)");
        } catch (SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    public static void exec(String sql, Object... params) {
        try (Connection c = PgInfra.admin(); PreparedStatement ps = c.prepareStatement(sql)) {
            for (int i = 0; i < params.length; i++) {
                ps.setObject(i + 1, params[i]);
            }
            ps.execute();
        } catch (SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    public static void countryRisk(String country, String level, String validFrom, String validTo) {
        exec("INSERT INTO risk.country_risk (country, level, source, list_date, valid_from, valid_to) "
                + "VALUES (?, ?, 'test', DATE '2026-01-01', CAST(? AS date), CAST(? AS date))", country, level,
                validFrom, validTo);
    }

    public static void exempt(UUID account) {
        exec("INSERT INTO risk.aml_exempt_accounts (account_id, reason) VALUES (?, 'CLEARING')", account);
    }
}
