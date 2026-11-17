package com.verabank.risk.stream;

import static com.verabank.risk.stream.StreamSupport.T0;
import static com.verabank.risk.stream.StreamSupport.published;
import static com.verabank.risk.stream.StreamSupport.sendAndWait;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.networknt.schema.JsonSchema;
import com.networknt.schema.JsonSchemaFactory;
import com.networknt.schema.SchemaLocation;
import com.networknt.schema.SchemaValidatorsConfig;
import com.networknt.schema.SpecVersion;
import com.verabank.risk.support.Dirs;
import io.quarkus.test.junit.QuarkusTest;
import io.quarkus.test.junit.TestProfile;
import java.time.Duration;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/** Los eventos de prueba y los que publica el servicio validan contra contracts/events (los archivos del repositorio). */
@QuarkusTest
@TestProfile(StreamProfile.class)
class ContractIT {

    private static final ObjectMapper JSON = new ObjectMapper();
    private static final JsonSchemaFactory FACTORY = JsonSchemaFactory.getInstance(SpecVersion.VersionFlag.V202012);
    private static final SchemaValidatorsConfig CONFIG = SchemaValidatorsConfig.builder().formatAssertionsEnabled(true).build();

    @BeforeEach
    void reset() {
        StreamDb.reset();
    }

    private static JsonSchema schema(String file) {
        return FACTORY.getSchema(SchemaLocation.of(Dirs.contracts().resolve("events").resolve(file).toUri().toString()), CONFIG);
    }

    private static void assertValid(String json, String payloadSchemaFile) throws Exception {
        JsonNode root = JSON.readTree(json);
        assertThat(schema("envelope.v1.json").validate(root)).as("sobre").isEmpty();
        assertThat(schema(payloadSchemaFile).validate(root.get("payload"))).as("payload").isEmpty();
    }

    @Test
    void theTestTransferCompletedEventsValidateAgainstTheirSchema() throws Exception {
        assertValid(Events.transfer(UUID.randomUUID(), "9500.00", T0).json(), "TransferCompleted.v1.schema.json");
        assertValid(Events.fraudLabel(UUID.randomUUID(), UUID.randomUUID(), true, "ANALYST"), "FraudLabelConfirmed.v1.schema.json");
    }

    @Test
    void thePublishedAmlAlertRaisedValidatesAgainstItsSchema() throws Exception {
        UUID source = UUID.randomUUID();
        for (int i = 0; i < 3; i++) {
            sendAndWait(Events.transfer(source, "9500.00", T0.plus(Duration.ofHours(i))));
        }
        sendAndWait(Events.transfer(source, "12000.00", T0.plus(Duration.ofHours(4))));
        var messages = published(source);
        assertThat(messages).hasSize(2);   // fraccionamiento + umbral
        for (var message : messages) {
            assertValid(message.value(), "AmlAlertRaised.v1.schema.json");
            JsonNode envelope = JSON.readTree(message.value());
            assertThat(envelope.get("event_type").asText()).isEqualTo("AmlAlertRaised");
            assertThat(envelope.get("aggregate_id").asText()).isEqualTo(envelope.at("/payload/alert_id").asText());
            assertThat(envelope.at("/payload/subject_account_id").asText()).isEqualTo(message.key());
            // Cabeceras como las del outbox de transfer-service.
            assertThat(new String(message.headers().lastHeader("event_id").value()))
                    .isEqualTo(envelope.get("event_id").asText());
            assertThat(new String(message.headers().lastHeader("event_type").value())).isEqualTo("AmlAlertRaised");
            assertThat(new String(message.headers().lastHeader("correlation_id").value()))
                    .isEqualTo(envelope.get("correlation_id").asText());
        }
        assertThat(messages.stream().map(m -> m.partition()).distinct()).hasSize(1);   // misma clave, misma partición
    }
}
