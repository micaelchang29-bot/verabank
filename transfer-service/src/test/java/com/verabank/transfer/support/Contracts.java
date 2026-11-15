package com.verabank.transfer.support;

import com.fasterxml.jackson.databind.JsonNode;
import com.networknt.schema.JsonSchema;
import com.networknt.schema.JsonSchemaFactory;
import com.networknt.schema.SchemaLocation;
import com.networknt.schema.SchemaValidatorsConfig;
import com.networknt.schema.SpecVersion;
import com.networknt.schema.ValidationMessage;
import java.nio.file.Path;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.stream.Collectors;

/**
 * Validación contra los contratos del monorepo, leídos de contracts/ sin copiarlos:
 * esquemas de eventos (JSON Schema 2020-12) y esquemas de componentes de openapi.yaml
 * (OpenAPI 3.1 usa el mismo dialecto). Los formatos (uuid, date-time) se validan.
 */
public final class Contracts {

    private static final Path DIR = Infra.repoPath("verabank.contracts.dir", "../contracts");
    private static final JsonSchemaFactory FACTORY = JsonSchemaFactory.getInstance(SpecVersion.VersionFlag.V202012);
    private static final SchemaValidatorsConfig CONFIG = SchemaValidatorsConfig.builder()
            .formatAssertionsEnabled(true)
            .build();
    private static final Map<String, JsonSchema> CACHE = new ConcurrentHashMap<>();

    private Contracts() {
    }

    /** Errores del sobre común (envelope.v1.json). */
    public static Set<String> envelopeErrors(JsonNode envelope) {
        return errors(event("envelope.v1.json"), envelope);
    }

    /** Errores del payload según contracts/events/{eventType}.v1.schema.json. */
    public static Set<String> payloadErrors(String eventType, JsonNode payload) {
        return errors(event(eventType + ".v1.schema.json"), payload);
    }

    /** Errores según un esquema de components/schemas de openapi.yaml. */
    public static Set<String> openApiErrors(String component, JsonNode value) {
        String uri = DIR.resolve("openapi.yaml").toUri() + "#/components/schemas/" + component;
        return errors(CACHE.computeIfAbsent(uri, u -> FACTORY.getSchema(SchemaLocation.of(u), CONFIG)), value);
    }

    private static JsonSchema event(String file) {
        String uri = DIR.resolve("events").resolve(file).toUri().toString();
        return CACHE.computeIfAbsent(uri, u -> FACTORY.getSchema(SchemaLocation.of(u), CONFIG));
    }

    private static Set<String> errors(JsonSchema schema, JsonNode value) {
        return schema.validate(value).stream().map(ValidationMessage::getMessage).collect(Collectors.toSet());
    }
}
