package com.verabank.transfer.saga;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.verabank.transfer.config.VerabankProperties;
import com.verabank.transfer.outbox.OutboxWriter;
import com.verabank.transfer.transfer.TransferRow;
import java.time.Instant;
import java.util.UUID;
import org.springframework.stereotype.Component;

/** Comandos al rail en rail.commands.v1, con clave transfer_id (docs/saga.md §4). */
@Component
public class RailEvents {

    private static final String AGGREGATE = "transfer";

    private final OutboxWriter outbox;
    private final ObjectMapper mapper;
    private final String topic;

    public RailEvents(OutboxWriter outbox, ObjectMapper mapper, VerabankProperties properties) {
        this.outbox = outbox;
        this.mapper = mapper;
        this.topic = properties.rail().commandsTopic();
    }

    public void creditInstruction(TransferRow t, UUID correlationId) {
        ObjectNode payload = mapper.createObjectNode();
        payload.put("transfer_id", t.transferId().toString());
        payload.put("dest_external_ref", t.destExternalRef());
        payload.put("dest_country", t.destCountry());
        payload.put("amount", t.amount().toPlainString());
        payload.put("currency", t.currency());
        emit(t, "CreditInstruction", payload, correlationId);
    }

    public void creditStatusQuery(TransferRow t, UUID correlationId) {
        emit(t, "CreditStatusQuery", idOnly(t), correlationId);
    }

    public void creditCancel(TransferRow t, UUID correlationId) {
        emit(t, "CreditCancel", idOnly(t), correlationId);
    }

    private ObjectNode idOnly(TransferRow t) {
        return mapper.createObjectNode().put("transfer_id", t.transferId().toString());
    }

    private void emit(TransferRow t, String eventType, ObjectNode payload, UUID correlationId) {
        outbox.append(topic, AGGREGATE, t.transferId(), t.transferId().toString(), eventType, Instant.now(),
                correlationId, payload);
    }
}
