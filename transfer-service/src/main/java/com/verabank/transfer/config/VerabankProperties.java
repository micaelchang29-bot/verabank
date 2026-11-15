package com.verabank.transfer.config;

import java.time.Duration;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.boot.context.properties.bind.DefaultValue;

@ConfigurationProperties(prefix = "verabank")
public record VerabankProperties(
        String migrationsDir,
        String apiKeys,
        Saga saga,
        Outbox outbox) {

    public record Saga(@DefaultValue("30s") Duration resumeAfter) {
    }

    public record Outbox(
            @DefaultValue("transfer.events.v1") String topic,
            @DefaultValue("true") boolean relayEnabled,
            @DefaultValue("100") long pollIntervalMs,
            @DefaultValue("200") int batchSize,
            @DefaultValue("815002") long lockKey,
            @DefaultValue("130s") Duration sendTimeout) {
    }

    /** No expone las claves en toString (los logs de arranque imprimen a veces la configuración). */
    @Override
    public String toString() {
        return "VerabankProperties[migrationsDir=" + migrationsDir + ", apiKeys=***, saga=" + saga
                + ", outbox=" + outbox + "]";
    }
}
