package com.verabank.risk.stream.aml;

import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.verabank.risk.shared.RiskConfig;
import io.quarkus.reactive.datasource.ReactiveDataSource;
import io.smallrye.mutiny.Uni;
import io.vertx.mutiny.pgclient.PgPool;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import java.time.Duration;
import java.util.HashMap;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.atomic.AtomicReference;
import org.jboss.logging.Logger;

/**
 * Versión activa de cada regla de {@code risk.aml_rules}, en caché y releída cada {@code verabank.stream.rules-refresh}
 * (60 s). Un cambio de parámetros es una versión nueva (db/README.md): la caché lo recoge en el siguiente refresco.
 */
@ApplicationScoped
public class RuleCatalog {

    private static final Logger LOG = Logger.getLogger(RuleCatalog.class);
    private static final String LOAD = "SELECT rule_code, version, params::text AS params FROM risk.aml_rules WHERE active";

    public record Snapshot(Map<String, RuleParams> rules, long loadedAtNanos) {

        public Optional<RuleParams> get(String ruleCode) {
            return Optional.ofNullable(rules.get(ruleCode));
        }
    }

    @Inject
    @ReactiveDataSource("stream")
    PgPool pool;
    @Inject
    RiskConfig config;
    @Inject
    ObjectMapper baseMapper;

    private final AtomicReference<Snapshot> current = new AtomicReference<>();
    private volatile Uni<Snapshot> loading;

    /** La instantánea vigente; si pasó el periodo de refresco la relee (una sola lectura aunque haya varios llamadores). */
    public Uni<Snapshot> snapshot() {
        Snapshot snapshot = current.get();
        if (snapshot != null && !expired(snapshot)) {
            return Uni.createFrom().item(snapshot);
        }
        return reload().onFailure().recoverWithItem(e -> {
            if (snapshot == null) {
                throw new IllegalStateException("no se pudieron leer las reglas AML", e);
            }
            LOG.warnf("no se pudo refrescar risk.aml_rules; se sigue con la caché: %s", e.toString());
            return snapshot;
        });
    }

    /** Fuerza la relectura (las pruebas la usan para no esperar 60 s). */
    public Uni<Snapshot> refresh() {
        return reload();
    }

    private boolean expired(Snapshot snapshot) {
        Duration refresh = config.stream().rulesRefresh();
        return System.nanoTime() - snapshot.loadedAtNanos() >= refresh.toNanos();
    }

    private synchronized Uni<Snapshot> reload() {
        if (loading == null) {
            loading = pool.query(LOAD).execute().map(rows -> {
                ObjectMapper mapper = baseMapper.copy().enable(DeserializationFeature.USE_BIG_DECIMAL_FOR_FLOATS);
                Map<String, RuleParams> rules = new HashMap<>();
                rows.forEach(row -> {
                    String code = row.getString("rule_code");
                    String json = row.getString("params");
                    try {
                        rules.put(code, new RuleParams(code, row.getInteger("version"), json, mapper.readTree(json)));
                    } catch (java.io.IOException e) {
                        throw new IllegalStateException("parámetros ilegibles de " + code, e);
                    }
                });
                Snapshot loaded = new Snapshot(Map.copyOf(rules), System.nanoTime());
                current.set(loaded);
                return loaded;
            }).onTermination().invoke(() -> {
                synchronized (RuleCatalog.this) {
                    loading = null;
                }
            }).memoize().indefinitely();
        }
        return loading;
    }
}
