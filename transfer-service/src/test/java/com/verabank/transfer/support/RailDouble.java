package com.verabank.transfer.support;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.function.Function;
import org.apache.kafka.clients.consumer.ConsumerConfig;
import org.apache.kafka.clients.consumer.KafkaConsumer;
import org.apache.kafka.clients.producer.KafkaProducer;
import org.apache.kafka.clients.producer.ProducerConfig;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.apache.kafka.common.TopicPartition;
import org.apache.kafka.common.serialization.StringDeserializer;
import org.apache.kafka.common.serialization.StringSerializer;

/**
 * Doble de prueba del rail: consume {@code rail.commands.v1} (solo lo que llega después de
 * arrancar), lo registra y responde en {@code rail.replies.v1} según la política vigente. Sin
 * política responde nada; la prueba puede además publicar respuestas a mano con {@link #send}.
 */
public final class RailDouble implements AutoCloseable {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    /** Comando recibido del servicio. */
    public record Command(String type, UUID transferId, UUID eventId, UUID correlationId, String key,
            JsonNode envelope) {
    }

    /** Respuesta a publicar; {@code eventId} nulo genera uno nuevo. */
    public record Reply(String eventType, UUID transferId, Map<String, Object> payload, UUID eventId) {

        public Reply withEventId(UUID id) {
            return new Reply(eventType, transferId, payload, id);
        }
    }

    public static Reply confirmed(UUID transferId) {
        return new Reply("CreditConfirmed", transferId, Map.of("transfer_id", transferId.toString(),
                "rail_ref", "RAIL-" + transferId.toString().substring(0, 8)), null);
    }

    public static Reply rejected(UUID transferId, String reasonCode) {
        return new Reply("CreditRejected", transferId,
                Map.of("transfer_id", transferId.toString(), "reason_code", reasonCode), null);
    }

    /** CreditStatus con el estado dado: CONFIRMED, REJECTED, CANCELLED o UNKNOWN. */
    public static Reply status(UUID transferId, String state) {
        return new Reply("CreditStatus", transferId,
                Map.of("transfer_id", transferId.toString(), "state", state), null);
    }

    private final KafkaProducer<String, String> producer;
    private final KafkaConsumer<String, String> consumer;
    private final List<Command> commands = new CopyOnWriteArrayList<>();
    private final Thread thread;
    private volatile Function<Command, List<Reply>> policy = c -> List.of();
    private volatile boolean running = true;

    private RailDouble() {
        String bootstrap = Infra.KAFKA.getBootstrapServers();
        this.producer = new KafkaProducer<>(Map.of(
                ProducerConfig.BOOTSTRAP_SERVERS_CONFIG, bootstrap,
                ProducerConfig.KEY_SERIALIZER_CLASS_CONFIG, StringSerializer.class,
                ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG, StringSerializer.class,
                ProducerConfig.ACKS_CONFIG, "all"));
        this.consumer = new KafkaConsumer<>(Map.of(
                ConsumerConfig.BOOTSTRAP_SERVERS_CONFIG, bootstrap,
                ConsumerConfig.GROUP_ID_CONFIG, "rail-double-" + UUID.randomUUID(),
                ConsumerConfig.KEY_DESERIALIZER_CLASS_CONFIG, StringDeserializer.class,
                ConsumerConfig.VALUE_DESERIALIZER_CLASS_CONFIG, StringDeserializer.class,
                ConsumerConfig.ENABLE_AUTO_COMMIT_CONFIG, false));
        List<TopicPartition> partitions = consumer.partitionsFor(Infra.RAIL_COMMANDS, Duration.ofSeconds(30)).stream()
                .map(p -> new TopicPartition(Infra.RAIL_COMMANDS, p.partition())).toList();
        consumer.assign(partitions);
        consumer.seekToEnd(partitions);
        partitions.forEach(consumer::position);   // fija la posición ya: no se pierde nada de lo que llegue después
        this.thread = new Thread(this::loop, "rail-double");
        this.thread.setDaemon(true);
    }

    public static RailDouble start() {
        RailDouble d = new RailDouble();
        d.thread.start();
        return d;
    }

    /** La política recibe cada comando y devuelve las respuestas que el rail publica. */
    public RailDouble respondWith(Function<Command, List<Reply>> policy) {
        this.policy = policy;
        return this;
    }

    public List<Command> commands() {
        return List.copyOf(commands);
    }

    public List<Command> commandsFor(UUID transferId) {
        return commands.stream().filter(c -> c.transferId().equals(transferId)).toList();
    }

    public long count(UUID transferId, String type) {
        return commandsFor(transferId).stream().filter(c -> c.type().equals(type)).count();
    }

    public void send(Reply reply) {
        publish(reply.transferId().toString(), envelope(reply).toString(), reply.eventType(),
                reply.eventId());
    }

    /** Publica un valor arbitrario (mensajes mal formados) con la clave dada. */
    public void sendRaw(String key, String value) {
        publish(key, value, "raw", null);
    }

    private void publish(String key, String value, String type, UUID eventId) {
        ProducerRecord<String, String> record = new ProducerRecord<>(Infra.RAIL_REPLIES, key, value);
        record.headers().add("event_type", type.getBytes(StandardCharsets.UTF_8));
        if (eventId != null) {
            record.headers().add("event_id", eventId.toString().getBytes(StandardCharsets.UTF_8));
        }
        try {
            producer.send(record).get();
        } catch (Exception e) {
            throw new IllegalStateException("no se pudo publicar la respuesta del rail", e);
        }
    }

    /** Sobre completo según envelope.v1.json; el id del evento queda en el propio sobre. */
    public static ObjectNode envelope(Reply reply) {
        ObjectNode e = MAPPER.createObjectNode();
        e.put("event_id", (reply.eventId() == null ? UUID.randomUUID() : reply.eventId()).toString());
        e.put("event_type", reply.eventType());
        e.put("schema_version", 1);
        e.put("aggregate_id", reply.transferId().toString());
        e.put("occurred_at", Instant.now().toString());
        e.put("emitted_at", Instant.now().toString());
        e.put("correlation_id", UUID.randomUUID().toString());
        e.set("payload", MAPPER.valueToTree(reply.payload()));
        return e;
    }

    private void loop() {
        try {
            while (running) {
                consumer.poll(Duration.ofMillis(100)).forEach(record -> {
                    try {
                        JsonNode envelope = MAPPER.readTree(record.value());
                        Command c = new Command(envelope.path("event_type").asText(),
                                UUID.fromString(envelope.path("payload").path("transfer_id").asText()),
                                UUID.fromString(envelope.path("event_id").asText()),
                                UUID.fromString(envelope.path("correlation_id").asText()), record.key(), envelope);
                        commands.add(c);
                        policy.apply(c).forEach(this::send);
                    } catch (Exception e) {
                        throw new IllegalStateException("comando ilegible: " + record.value(), e);
                    }
                });
            }
        } catch (RuntimeException e) {
            if (running) {
                e.printStackTrace();
            }
        } finally {
            consumer.close(Duration.ZERO);
        }
    }

    @Override
    public void close() {
        running = false;
        try {
            thread.join(5000);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        producer.close(Duration.ofSeconds(2));
    }
}
