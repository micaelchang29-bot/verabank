package com.verabank.risk.stream.messaging;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.networknt.schema.JsonSchema;
import com.networknt.schema.JsonSchemaFactory;
import com.networknt.schema.SchemaLocation;
import com.networknt.schema.SchemaValidatorsConfig;
import com.networknt.schema.SpecVersion;
import com.networknt.schema.ValidationMessage;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.stream.Collectors;

/**
 * Valida el envoltorio y el payload contra contracts/events (JSON Schema 2020-12, con {@code format} verificado).
 * Los esquemas viajan en el jar, copiados de contracts/ al compilar (pom.xml).
 */
@ApplicationScoped
public class EventSchemas {

    private static final String BASE = "contracts/events/";
    private static final Set<String> PAYLOADS = Set.of("TransferCompleted", "FraudLabelConfirmed");

    @Inject
    ObjectMapper mapper;

    private final Map<String, JsonSchema> schemas = new HashMap<>();
    private JsonSchema envelope;

    synchronized void ensureLoaded() {
        if (envelope != null) {
            return;
        }
        JsonSchemaFactory factory = JsonSchemaFactory.getInstance(SpecVersion.VersionFlag.V202012);
        SchemaValidatorsConfig config = SchemaValidatorsConfig.builder().formatAssertionsEnabled(true).build();
        envelope = load(factory, config, "envelope.v1.json");
        for (String type : PAYLOADS) {
            schemas.put(type, load(factory, config, type + ".v1.schema.json"));
        }
    }

    private static JsonSchema load(JsonSchemaFactory factory, SchemaValidatorsConfig config, String file) {
        if (Thread.currentThread().getContextClassLoader().getResource(BASE + file) == null) {
            throw new IllegalStateException("falta el esquema " + BASE + file + " en el classpath");
        }
        return factory.getSchema(SchemaLocation.of("classpath:" + BASE + file), config);
    }

    /** Valida el envoltorio. Lanza {@link InvalidMessageException} si el mensaje no es JSON o no lo cumple. */
    public Envelope parseEnvelope(String value) {
        ensureLoaded();
        JsonNode root;
        try {
            root = mapper.readTree(value == null ? "" : value);
        } catch (JsonProcessingException e) {
            throw new InvalidMessageException("el mensaje no es JSON: " + e.getOriginalMessage());
        }
        if (root == null || !root.isObject()) {
            throw new InvalidMessageException("el mensaje no es un objeto JSON");
        }
        check("envoltorio", envelope.validate(root));
        return new Envelope(UUID.fromString(root.path("event_id").asText()), root.path("event_type").asText(),
                UUID.fromString(root.path("aggregate_id").asText()),
                OffsetDateTime.parse(root.path("occurred_at").asText()).toInstant(),
                OffsetDateTime.parse(root.path("emitted_at").asText()).toInstant(),
                UUID.fromString(root.path("correlation_id").asText()), root.path("payload"));
    }

    /** Valida el payload del tipo dado; el tipo debe tener esquema en este servicio. */
    public void validatePayload(Envelope envelope) {
        ensureLoaded();
        JsonSchema schema = schemas.get(envelope.eventType());
        if (schema == null) {
            throw new InvalidMessageException("event_type sin esquema en este consumidor: " + envelope.eventType());
        }
        check("payload de " + envelope.eventType(), schema.validate(envelope.payload()));
    }

    /** Fecha de instante tal como la persiste PostgreSQL (microsegundos). */
    public static Instant micros(Instant instant) {
        return instant.truncatedTo(java.time.temporal.ChronoUnit.MICROS);
    }

    private static void check(String what, Set<ValidationMessage> errors) {
        if (!errors.isEmpty()) {
            throw new InvalidMessageException(what + " no cumple su esquema: "
                    + errors.stream().map(ValidationMessage::getMessage).sorted().collect(Collectors.joining("; ")));
        }
    }
}
