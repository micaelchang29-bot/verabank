package com.verabank.transfer.saga;

import java.util.UUID;

/** Respuesta del rail ya validada contra su esquema. */
public record RailReply(UUID eventId, String eventType, UUID transferId, UUID correlationId, ReplyKind kind,
        String failReason) {
}
