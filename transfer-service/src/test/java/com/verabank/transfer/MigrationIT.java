package com.verabank.transfer;

import static org.assertj.core.api.Assertions.assertThat;

import com.verabank.transfer.support.Infra;
import com.verabank.transfer.support.IntegrationTest;
import java.util.List;
import org.junit.jupiter.api.Test;

/** Las migraciones reales de db/migrations se aplican con la configuración de db/README.md. */
class MigrationIT extends IntegrationTest {

    @Test
    void coreYAuditEnUnaCorridaConHistorialEnFlywayTransfer() {
        List<String> versions = jdbc.sql("""
                SELECT version FROM flyway_transfer.flyway_schema_history
                WHERE version IS NOT NULL AND success ORDER BY installed_rank""")
                .query(String.class).list();
        assertThat(versions).containsExactly(
                "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13");
    }

    @Test
    void elRolDelServicioEsPropietarioDeCoreYAudit() {
        List<String> owners = jdbc.sql("""
                SELECT n.nspname || ':' || pg_get_userbyid(n.nspowner) FROM pg_namespace n
                WHERE n.nspname IN ('core', 'audit', 'flyway_transfer') ORDER BY 1""")
                .query(String.class).list();
        assertThat(owners).containsExactly(
                "audit:" + Infra.TRANSFER_USER,
                "core:" + Infra.TRANSFER_USER,
                "flyway_transfer:" + Infra.TRANSFER_USER);
        assertThat(jdbc.sql("SELECT current_user").query(String.class).single())
                .isEqualTo(Infra.TRANSFER_USER);
    }

    /** V13 no depende de roles que solo existen cuando risk está desplegado. */
    @Test
    void auditAppendSeCreaSinElRolDeRiskYSoloElDuenoPuedeEjecutarla() {
        assertThat(jdbc.sql("SELECT count(*) FROM pg_roles WHERE rolname IN ('risk_svc', 'sim_loader')")
                .query(Integer.class).single()).isZero();
        assertThat(jdbc.sql("""
                SELECT p.prosecdef AND p.proconfig::text LIKE '%search_path=pg_catalog, pg_temp%'
                FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'audit' AND p.proname = 'append'""")
                .query(Boolean.class).single()).isTrue();
        assertThat(jdbc.sql("""
                SELECT has_function_privilege('public', 'audit.append(text,text,text,text,uuid,jsonb)', 'EXECUTE')""")
                .query(Boolean.class).single()).isFalse();
    }
}
