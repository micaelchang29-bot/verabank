package com.verabank.risk.support;

import java.time.Duration;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.UUID;
import java.util.concurrent.ExecutionException;
import org.apache.kafka.clients.admin.Admin;
import org.apache.kafka.clients.admin.NewTopic;
import org.apache.kafka.clients.consumer.ConsumerConfig;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.apache.kafka.clients.consumer.KafkaConsumer;
import org.apache.kafka.clients.producer.KafkaProducer;
import org.apache.kafka.clients.producer.ProducerConfig;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.apache.kafka.common.TopicPartition;
import org.apache.kafka.common.serialization.StringDeserializer;
import org.apache.kafka.common.serialization.StringSerializer;
import org.testcontainers.kafka.KafkaContainer;

/** Kafka compartido por la suite (una vez por JVM), con los tópicos de docs/diseno.md §3.1 y sus DLT de una partición. */
public final class KafkaInfra {

    public static final String TRANSFER_EVENTS = "transfer.events.v1";
    public static final String FRAUD_LABELS = "fraud.labels.v1";
    public static final String AML_ALERTS = "aml.alerts.v1";

    public static final KafkaContainer KAFKA;

    static {
        KAFKA = new KafkaContainer("apache/kafka:4.3.2").withEnv("KAFKA_AUTO_CREATE_TOPICS_ENABLE", "false");
        KAFKA.start();
        try (Admin admin = Admin.create(Map.of("bootstrap.servers", KAFKA.getBootstrapServers()))) {
            admin.createTopics(List.of(
                    new NewTopic(TRANSFER_EVENTS, 6, (short) 1), new NewTopic(FRAUD_LABELS, 3, (short) 1),
                    new NewTopic(AML_ALERTS, 3, (short) 1),
                    new NewTopic(TRANSFER_EVENTS + ".dlt", 1, (short) 1),
                    new NewTopic(FRAUD_LABELS + ".dlt", 1, (short) 1))).all().get();
        } catch (InterruptedException | ExecutionException e) {
            throw new IllegalStateException("no se pudieron crear los tópicos de prueba", e);
        }
    }

    private static KafkaProducer<String, String> producer;

    private KafkaInfra() {
    }

    public static void init() {
        // Fuerza la carga de la clase (y el arranque del contenedor).
    }

    public static String bootstrap() {
        return KAFKA.getBootstrapServers();
    }

    public static synchronized void send(String topic, String key, String value) {
        try {
            if (producer == null) {
                producer = new KafkaProducer<>(Map.of(ProducerConfig.BOOTSTRAP_SERVERS_CONFIG, bootstrap(),
                        ProducerConfig.ACKS_CONFIG, "all"), new StringSerializer(), new StringSerializer());
            }
            producer.send(new ProducerRecord<>(topic, key, value)).get();
        } catch (InterruptedException | ExecutionException e) {
            throw new IllegalStateException(e);
        }
    }

    /** Lee desde el principio todo lo que hay ahora en el tópico (cada llamada con su propio grupo). */
    public static List<ConsumerRecord<String, String>> readAll(String topic) {
        Properties props = new Properties();
        props.put(ConsumerConfig.BOOTSTRAP_SERVERS_CONFIG, bootstrap());
        props.put(ConsumerConfig.GROUP_ID_CONFIG, "test-reader-" + UUID.randomUUID());
        props.put(ConsumerConfig.AUTO_OFFSET_RESET_CONFIG, "earliest");
        props.put(ConsumerConfig.ENABLE_AUTO_COMMIT_CONFIG, "false");
        List<ConsumerRecord<String, String>> out = new ArrayList<>();
        try (KafkaConsumer<String, String> consumer = new KafkaConsumer<>(props, new StringDeserializer(),
                new StringDeserializer())) {
            Collection<TopicPartition> partitions = consumer.partitionsFor(topic).stream()
                    .map(p -> new TopicPartition(topic, p.partition())).toList();
            consumer.assign(partitions);
            consumer.seekToBeginning(partitions);
            Map<TopicPartition, Long> end = consumer.endOffsets(partitions);
            while (end.entrySet().stream().anyMatch(e -> consumer.position(e.getKey()) < e.getValue())) {
                consumer.poll(Duration.ofMillis(200)).forEach(out::add);
            }
        }
        return out;
    }
}
