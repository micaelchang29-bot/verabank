package com.verabank.transfer.transfer;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.verabank.transfer.config.VerabankProperties;
import com.verabank.transfer.outbox.OutboxWriter;
import com.verabank.transfer.scoring.ScoreResult;
import java.util.UUID;
import org.springframework.stereotype.Component;

/**
 * Eventos de transfer.events.v1 (contracts/events), con clave source_account_id (ADR-06).
 * Los campos opcionales se omiten en lugar de ir en null: los esquemas los declaran string.
 */
@Component
public class TransferEvents {

    static final String AGGREGATE = "transfer";

    private final OutboxWriter outbox;
    private final ObjectMapper mapper;
    private final String topic;

    public TransferEvents(OutboxWriter outbox, ObjectMapper mapper, VerabankProperties properties) {
        this.outbox = outbox;
        this.mapper = mapper;
        this.topic = properties.outbox().topic();
    }

    public void initiated(TransferRow t, UUID correlationId) {
        emit(t, "TransferInitiated", describe(t), correlationId);
    }

    /** Resultado del scoring (§3.3); se emite en tx2 justo después de guardar el score. */
    public void scored(TransferRow t, ScoreResult score, UUID correlationId) {
        ObjectNode payload = mapper.createObjectNode();
        payload.put("transfer_id", t.transferId().toString());
        payload.put("score", score.score().doubleValue());
        payload.put("decision", score.decision().name());
        payload.put("decision_source", score.source().name());
        payload.put("model_version", score.modelVersion());
        payload.put("latency_ms", score.latencyMs());
        emit(t, "TransferScored", payload, correlationId);
    }

    public void completed(TransferRow t, UUID correlationId) {
        ObjectNode payload = describe(t);
        payload.put("completed_at", t.completedAt().toString());
        emit(t, "TransferCompleted", payload, correlationId);
    }

    public void rejected(TransferRow t, RejectionReason reason, UUID correlationId) {
        ObjectNode payload = mapper.createObjectNode();
        payload.put("transfer_id", t.transferId().toString());
        payload.put("reason", reason.name());
        emit(t, "TransferRejected", payload, correlationId);
    }

    /** Compensación cumplida (S10): la retención ya se liberó. */
    public void failed(TransferRow t, String reason, UUID correlationId) {
        ObjectNode payload = mapper.createObjectNode();
        payload.put("transfer_id", t.transferId().toString());
        payload.put("reason", reason);
        payload.put("compensated", true);
        emit(t, "TransferFailed", payload, correlationId);
    }

    public void held(TransferRow t, UUID correlationId) {
        ObjectNode payload = mapper.createObjectNode();
        payload.put("transfer_id", t.transferId().toString());
        payload.put("reason", "FRAUD_REVIEW");
        emit(t, "TransferHeld", payload, correlationId);
    }

    private ObjectNode describe(TransferRow t) {
        ObjectNode p = mapper.createObjectNode();
        p.put("transfer_id", t.transferId().toString());
        p.put("source_account_id", t.sourceAccountId().toString());
        if (t.destAccountId() != null) {
            p.put("dest_account_id", t.destAccountId().toString());
        }
        if (t.destExternalRef() != null) {
            p.put("dest_external_ref", t.destExternalRef());
        }
        p.put("dest_country", t.destCountry());
        p.put("rail", t.rail().name());
        p.put("transfer_type", t.transferType().name());
        p.put("amount", t.amount().toPlainString());
        p.put("currency", t.currency());
        return p;
    }

    private void emit(TransferRow t, String eventType, ObjectNode payload, UUID correlationId) {
        outbox.append(topic, AGGREGATE, t.transferId(), t.sourceAccountId().toString(), eventType, t.occurredAt(),
                correlationId, payload);
    }
}
