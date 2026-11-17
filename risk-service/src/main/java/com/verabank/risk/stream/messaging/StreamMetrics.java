package com.verabank.risk.stream.messaging;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import java.time.Duration;

/** Métricas del rol stream. El retraso de offsets lo da el binder de Kafka (kafka_consumer_fetch_manager_*). */
@ApplicationScoped
public class StreamMetrics {

    @Inject
    MeterRegistry registry;

    /** Retraso del consumidor: reloj real al empezar a procesar menos {@code emitted_at} del evento. */
    public void consumerDelay(String topic, Duration delay) {
        Timer.builder("stream.consumer.delay").description("Ahora menos emitted_at al procesar el mensaje")
                .tag("topic", topic).publishPercentileHistogram().register(registry)
                .record(delay.isNegative() ? Duration.ZERO : delay);
    }

    /** Alertas nuevas por regla. */
    public void alertRaised(String ruleCode) {
        Counter.builder("aml.alerts.raised").description("Alertas AML creadas, por regla").tag("rule_code", ruleCode)
                .register(registry).increment();
    }

    /** Alertas existentes cuyo conjunto de transferencias cambió. */
    public void alertUpdated(String ruleCode) {
        Counter.builder("aml.alerts.updated").description("Alertas AML ampliadas, por regla")
                .tag("rule_code", ruleCode).register(registry).increment();
    }

    /** Latencia de persistencia: created_at menos trigger_emitted_at (§1.2: p95 &lt; 5 s). */
    public void persistLatency(Duration latency) {
        Timer.builder("aml.alert.persist.latency").description("created_at menos trigger_emitted_at de la alerta")
                .serviceLevelObjectives(Duration.ofMillis(100), Duration.ofMillis(500), Duration.ofSeconds(1),
                        Duration.ofMillis(2500), Duration.ofSeconds(5))
                .register(registry).record(latency.isNegative() ? Duration.ZERO : latency);
    }

    public void deadLettered(String topic, String reason) {
        Counter.builder("stream.dlt").description("Mensajes enviados al .dlt").tag("topic", topic)
                .tag("reason", reason).register(registry).increment();
    }

    public void attempt(String consumer) {
        Counter.builder("stream.processing.attempts").description("Intentos de proceso de un mensaje")
                .tag("consumer", consumer).register(registry).increment();
    }

    public void duplicate(String consumer) {
        Counter.builder("stream.inbox.duplicates").description("Mensajes ya procesados (inbox)")
                .tag("consumer", consumer).register(registry).increment();
    }

    public void publishFailure() {
        Counter.builder("aml.alerts.publish.failures").description("AmlAlertRaised que no se pudo publicar")
                .register(registry).increment();
    }
}
