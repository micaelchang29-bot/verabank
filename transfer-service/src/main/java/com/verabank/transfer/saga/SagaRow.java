package com.verabank.transfer.saga;

import com.fasterxml.jackson.databind.JsonNode;
import java.time.Instant;
import java.util.UUID;

/** Fila de core.saga_state (docs/saga.md §2). */
public record SagaRow(
        UUID sagaId,
        String currentStep,
        String status,
        int attempt,
        Instant wakeAt,
        String lastError,
        JsonNode payload,
        long version,
        Instant createdAt,
        Instant updatedAt) {

    public long payloadLong(String name) {
        return payload.path(name).asLong(0);
    }

    public String payloadText(String name) {
        JsonNode v = payload.path(name);
        return v.isMissingNode() || v.isNull() ? null : v.asText();
    }

    public boolean is(String status, String step) {
        return this.status.equals(status) && this.currentStep.equals(step);
    }
}
