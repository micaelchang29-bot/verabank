package com.verabank.risk.support;

import java.math.BigDecimal;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.UUID;

/** Atajos JDBC (como administrador) para preparar y leer la bitácora en las pruebas. */
public final class Db {

    private Db() {
    }

    public static void insertHistory(String source, String dest, String amount, Instant at, String modelVersion) {
        try (Connection c = PgInfra.admin(); PreparedStatement ps = c.prepareStatement("""
                INSERT INTO risk.scoring_log (transfer_id, source_ref, dest_ref, amount, occurred_at, features, score, decision, model_version)
                VALUES (?, ?, ?, ?, ?, '{}'::jsonb, 0.10000, 'APPROVE', ?)""")) {
            ps.setObject(1, UUID.randomUUID());
            ps.setString(2, source);
            ps.setString(3, dest);
            ps.setBigDecimal(4, new BigDecimal(amount));
            ps.setTimestamp(5, Timestamp.from(at));
            ps.setString(6, modelVersion);
            ps.executeUpdate();
        } catch (SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    public static long count(UUID transferId) {
        return queryLong("SELECT count(*) FROM risk.scoring_log WHERE transfer_id = ?", transferId);
    }

    public static long queryLong(String sql, Object... params) {
        try (Connection c = PgInfra.admin(); PreparedStatement ps = c.prepareStatement(sql)) {
            for (int i = 0; i < params.length; i++) {
                ps.setObject(i + 1, params[i]);
            }
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getLong(1);
            }
        } catch (SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    public static String queryString(String sql, Object... params) {
        try (Connection c = PgInfra.admin(); PreparedStatement ps = c.prepareStatement(sql)) {
            for (int i = 0; i < params.length; i++) {
                ps.setObject(i + 1, params[i]);
            }
            try (ResultSet rs = ps.executeQuery()) {
                return rs.next() ? rs.getString(1) : null;
            }
        } catch (SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    public static BigDecimal queryDecimal(String sql, Object... params) {
        String value = queryString(sql, params);
        return value == null ? null : new BigDecimal(value);
    }
}
