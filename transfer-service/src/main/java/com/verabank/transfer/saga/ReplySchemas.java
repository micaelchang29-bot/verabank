package com.verabank.transfer.saga;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.networknt.schema.JsonSchema;
import com.networknt.schema.JsonSchemaFactory;
import com.networknt.schema.SchemaLocation;
import com.networknt.schema.SchemaValidatorsConfig;
import com.networknt.schema.SpecVersion;
import com.networknt.schema.ValidationMessage;
import com.verabank.transfer.config.VerabankProperties;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.stream.Collectors;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

/**
 * Valida y descompone las respuestas del rail contra contracts/events (sobre y payload). Los
 * esquemas se leen de {@code verabank.contracts-dir}, igual que las migraciones, sin copiarse al servicio.
 */
@Component
@ConditionalOnProperty(prefix = "verabank.rail", name = "replies-enabled", havingValue = "true",
        matchIfMissing = true)
public class ReplySchemas {

    private static final Set<String> REPLY_TYPES = Set.of("CreditConfirmed", "CreditRejected", "CreditStatus");

    private final ObjectMapper mapper;
    private final JsonSchema envelope;
    private final Map<String, JsonSchema> payloads = new HashMap<>();

    public ReplySchemas(ObjectMapper mapper, VerabankProperties properties) {
        this.mapper = mapper;
        Path dir = Path.of(properties.contractsDir()).toAbsolutePath().normalize().resolve("events");
        if (!Files.isDirectory(dir)) {
            throw new IllegalStateException("no existe el directorio de esquemas " + dir
                    + " (verabank.contracts-dir / VERABANK_CONTRACTS_DIR)");
        }
        JsonSchemaFactory factory = JsonSchemaFactory.getInstance(SpecVersion.VersionFlag.V202012);
        SchemaValidatorsConfig config = SchemaValidatorsConfig.builder().formatAssertionsEnabled(true).build();
        this.envelope = factory.getSchema(SchemaLocation.of(dir.resolve("envelope.v1.json").toUri().toString()),
                config);
        for (String type : REPLY_TYPES) {
            payloads.put(type, factory.getSchema(
                    SchemaLocation.of(dir.resolve(type + ".v1.schema.json").toUri().toString()), config));
        }
    }

    /** Parsea y valida el mensaje; lanza {@link InvalidReplyException} si no cumple el contrato. */
    public RailReply parse(String value) {
        JsonNode root;
        try {
            root = mapper.readTree(value == null ? "" : value);
        } catch (JsonProcessingException e) {
            throw new InvalidReplyException("el mensaje no es JSON: " + e.getOriginalMessage());
        }
        if (root == null || !root.isObject()) {
            throw new InvalidReplyException("el mensaje no es un objeto JSON");
        }
        check("sobre", envelope.validate(root));
        String type = root.path("event_type").asText();
        JsonSchema payloadSchema = payloads.get(type);
        if (payloadSchema == null) {
            throw new InvalidReplyException("event_type no admitido en rail.replies.v1: " + type);
        }
        JsonNode payload = root.path("payload");
        check("payload de " + type, payloadSchema.validate(payload));

        ReplyKind kind;
        String failReason = null;
        switch (type) {
            case "CreditConfirmed" -> kind = ReplyKind.CONFIRM;
            case "CreditRejected" -> {
                kind = ReplyKind.REJECT;
                failReason = "RAIL_REJECTED";
            }
            default -> {
                String state = payload.path("state").asText();
                switch (state) {
                    case "CONFIRMED" -> kind = ReplyKind.CONFIRM;
                    case "REJECTED" -> {
                        kind = ReplyKind.REJECT;
                        failReason = "RAIL_REJECTED";
                    }
                    case "CANCELLED" -> {
                        kind = ReplyKind.REJECT;
                        failReason = "RAIL_CANCELLED";
                    }
                    default -> kind = ReplyKind.UNKNOWN;
                }
            }
        }
        return new RailReply(UUID.fromString(root.path("event_id").asText()), type,
                UUID.fromString(payload.path("transfer_id").asText()),
                UUID.fromString(root.path("correlation_id").asText()), kind, failReason);
    }

    private static void check(String what, Set<ValidationMessage> errors) {
        if (!errors.isEmpty()) {
            throw new InvalidReplyException(what + " no cumple su esquema: "
                    + errors.stream().map(ValidationMessage::getMessage).sorted().collect(Collectors.joining("; ")));
        }
    }
}
