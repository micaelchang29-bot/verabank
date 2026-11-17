package com.verabank.risk.stream.messaging;

import com.fasterxml.jackson.databind.JsonNode;
import java.time.Instant;
import java.util.UUID;

/** Envoltorio común de contracts/events/envelope.v1.json, ya validado. */
public record Envelope(UUID eventId, String eventType, UUID aggregateId, Instant occurredAt, Instant emittedAt,
        UUID correlationId, JsonNode payload) {
}
