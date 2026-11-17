package com.verabank.risk.stream.messaging;

import io.smallrye.mutiny.Uni;
import io.smallrye.mutiny.infrastructure.Infrastructure;
import jakarta.annotation.PreDestroy;
import jakarta.enterprise.context.ApplicationScoped;
import java.time.Duration;
import java.util.Map;
import org.apache.kafka.clients.admin.Admin;
import org.apache.kafka.clients.admin.AdminClientConfig;
import org.apache.kafka.clients.producer.KafkaProducer;
import org.apache.kafka.clients.producer.ProducerConfig;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.apache.kafka.common.serialization.StringSerializer;
import org.eclipse.microprofile.config.inject.ConfigProperty;

/**
 * Productor idempotente ({@code enable.idempotence=true}, {@code acks=all}) para {@code aml.alerts.v1} y los
 * {@code .dlt}, y cliente de administración para la readiness. Se crean al primer uso: el rol scoring no abre ninguna
 * conexión a Kafka. Las llamadas que pueden bloquear (metadatos) se ejecutan en el pool de workers.
 */
@ApplicationScoped
public class KafkaGateway {

    @ConfigProperty(name = "kafka.bootstrap.servers")
    String bootstrap;

    private KafkaProducer<String, String> producer;
    private Admin admin;

    private synchronized KafkaProducer<String, String> producer() {
        if (producer == null) {
            producer = new KafkaProducer<>(Map.of(
                    ProducerConfig.BOOTSTRAP_SERVERS_CONFIG, bootstrap,
                    ProducerConfig.CLIENT_ID_CONFIG, "risk-stream-producer",
                    ProducerConfig.ENABLE_IDEMPOTENCE_CONFIG, true,
                    ProducerConfig.ACKS_CONFIG, "all",
                    ProducerConfig.MAX_BLOCK_MS_CONFIG, 10_000,
                    ProducerConfig.DELIVERY_TIMEOUT_MS_CONFIG, 30_000,
                    ProducerConfig.REQUEST_TIMEOUT_MS_CONFIG, 10_000),
                    new StringSerializer(), new StringSerializer());
        }
        return producer;
    }

    private synchronized Admin admin() {
        if (admin == null) {
            admin = Admin.create(Map.of(AdminClientConfig.BOOTSTRAP_SERVERS_CONFIG, bootstrap,
                    AdminClientConfig.CLIENT_ID_CONFIG, "risk-stream-admin",
                    AdminClientConfig.REQUEST_TIMEOUT_MS_CONFIG, 2_000,
                    AdminClientConfig.DEFAULT_API_TIMEOUT_MS_CONFIG, 2_000));
        }
        return admin;
    }

    /** Envía y espera la confirmación del broker (acks=all). */
    public Uni<Void> send(ProducerRecord<String, String> record) {
        return Uni.createFrom().<Void>emitter(emitter -> {
            try {
                producer().send(record, (metadata, error) -> {
                    if (error == null) {
                        emitter.complete(null);
                    } else {
                        emitter.fail(error);
                    }
                });
            } catch (RuntimeException e) {
                emitter.fail(e);
            }
        }).runSubscriptionOn(Infrastructure.getDefaultWorkerPool());
    }

    /** Falla si el clúster no responde en el plazo dado. */
    public Uni<Void> ping(Duration timeout) {
        return Uni.createFrom().<Void>item(() -> {
            try {
                admin().describeCluster().nodes().get(timeout.toMillis(), java.util.concurrent.TimeUnit.MILLISECONDS);
                return null;
            } catch (java.util.concurrent.ExecutionException | java.util.concurrent.TimeoutException e) {
                throw new IllegalStateException("Kafka no responde", e);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new IllegalStateException(e);
            }
        }).runSubscriptionOn(Infrastructure.getDefaultWorkerPool());
    }

    @PreDestroy
    synchronized void close() {
        if (producer != null) {
            producer.close(Duration.ofSeconds(2));
        }
        if (admin != null) {
            admin.close(Duration.ofSeconds(2));
        }
    }
}
