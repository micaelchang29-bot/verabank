package com.verabank.transfer.config;

import java.time.Duration;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.boot.context.properties.bind.DefaultValue;

@ConfigurationProperties(prefix = "verabank")
public record VerabankProperties(
        String migrationsDir,
        String contractsDir,
        String apiKeys,
        Saga saga,
        Rail rail,
        Outbox outbox) {

    /** Temporizadores y reintentos de la saga: docs/saga.md §3. */
    public record Saga(
            @DefaultValue("30s") Duration resumeAfter,
            @DefaultValue("30s") Duration railReplyTimeout,
            @DefaultValue("10s") Duration pollBackoffBase,
            @DefaultValue("60s") Duration pollBackoffMax,
            @DefaultValue("3") int cancelAfterPolls,
            @DefaultValue("1s") Duration backoffBase,
            @DefaultValue("60s") Duration backoffMax,
            @DefaultValue("5") int maxAttempts,
            @DefaultValue("1s") Duration pollerInterval,
            @DefaultValue("50") int pollerBatch,
            @DefaultValue("15m") Duration stuckAfter,
            @DefaultValue("15s") Duration stuckRefresh,
            @DefaultValue("true") boolean pollerEnabled) {
    }

    /** Mensajería con el rail (docs/saga.md §4). */
    public record Rail(
            @DefaultValue("rail.commands.v1") String commandsTopic,
            @DefaultValue("rail.replies.v1") String repliesTopic,
            @DefaultValue("true") boolean repliesEnabled,
            @DefaultValue("1s") Duration retryInterval,
            @DefaultValue("3") int retries) {
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
                + ", rail=" + rail + ", outbox=" + outbox + "]";
    }
}
