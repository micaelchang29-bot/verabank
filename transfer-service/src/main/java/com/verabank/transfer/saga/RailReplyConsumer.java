package com.verabank.transfer.saga;

import com.verabank.transfer.transfer.Rail;
import com.verabank.transfer.transfer.TransferRepository;
import com.verabank.transfer.transfer.TransferRow;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.kafka.support.Acknowledgment;
import org.springframework.stereotype.Component;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * Consumidor de rail.replies.v1 (docs/saga.md §4 y §5): grupo {@code transfer-service}, commit manual
 * después de confirmar la transacción local y, en esa misma transacción, el registro en el inbox
 * junto al efecto.
 *
 * <p>Errores: un conflicto de versión se reintenta releyendo la saga; los fallos no reintentables
 * ({@link InvalidReplyException}, {@link UnknownTransferException}) van directo al DLT; el resto
 * se reintenta con la política del {@link RailRepliesErrorConfig} y luego va al DLT.
 */
@Component
@ConditionalOnProperty(prefix = "verabank.rail", name = "replies-enabled", havingValue = "true",
        matchIfMissing = true)
public class RailReplyConsumer {

    private static final Logger log = LoggerFactory.getLogger(RailReplyConsumer.class);

    public static final String INBOX_CONSUMER = "transfer-service:rail-replies";
    static final int MAX_VERSION_CONFLICTS = 5;

    private final ReplySchemas schemas;
    private final InboxRepository inbox;
    private final TransferRepository transfers;
    private final SagaRepository sagas;
    private final RailSaga railSaga;
    private final SagaMetrics metrics;
    private final TransactionTemplate tx;

    public RailReplyConsumer(ReplySchemas schemas, InboxRepository inbox, TransferRepository transfers,
            SagaRepository sagas, RailSaga railSaga, SagaMetrics metrics, PlatformTransactionManager txManager) {
        this.schemas = schemas;
        this.inbox = inbox;
        this.transfers = transfers;
        this.sagas = sagas;
        this.railSaga = railSaga;
        this.metrics = metrics;
        this.tx = new TransactionTemplate(txManager);
        this.tx.setIsolationLevel(TransactionDefinition.ISOLATION_READ_COMMITTED);
    }

    @KafkaListener(id = "rail-replies", topics = "${verabank.rail.replies-topic:rail.replies.v1}",
            groupId = "transfer-service")
    public void onMessage(ConsumerRecord<String, String> record, Acknowledgment ack) {
        RailReply reply = schemas.parse(record.value());
        MDC.put("correlation_id", reply.correlationId().toString());
        try {
            process(reply);
            ack.acknowledge();   // solo tras confirmar la transacción local
        } finally {
            MDC.remove("correlation_id");
        }
    }

    /** Procesa una respuesta ya validada; un conflicto de versión relee la saga y repite. */
    public void process(RailReply reply) {
        for (int attempt = 1;; attempt++) {
            try {
                RailSaga.Result result = tx.execute(s -> handle(reply));
                after(reply, result);
                return;
            } catch (SagaConflictException e) {
                if (attempt >= MAX_VERSION_CONFLICTS) {
                    throw e;
                }
                log.debug("conflicto de versión en la saga {} (intento {}); se relee y se repite",
                        reply.transferId(), attempt);
            }
        }
    }

    private RailSaga.Result handle(RailReply reply) {
        if (!inbox.markProcessed(INBOX_CONSUMER, reply.eventId())) {
            return null;   // ya procesado: sin efectos
        }
        TransferRow transfer = transfers.find(reply.transferId()).orElseThrow(
                () -> new UnknownTransferException(reply.transferId()));
        if (transfer.rail() != Rail.EXTERNAL) {
            throw new InvalidReplyException("la transferencia " + reply.transferId() + " es INTERNAL");
        }
        SagaRow saga = sagas.find(reply.transferId()).orElseThrow(
                () -> new UnknownTransferException(reply.transferId()));
        return railSaga.onReply(reply, saga, transfer);
    }

    /** Métricas y S10, ya confirmada la transacción. */
    private void after(RailReply reply, RailSaga.Result result) {
        if (result == null) {
            metrics.inboxDuplicates.increment();
            log.info("respuesta {} ya procesada (event_id {}); sin efectos", reply.eventType(), reply.eventId());
            return;
        }
        switch (result) {
            case DUPLICATE -> {
                metrics.inboxDuplicates.increment();
                log.info("{} repetida para la transferencia {} (event_id {}) con la saga ya completada; sin cambios",
                        reply.eventType(), reply.transferId(), reply.eventId());
            }
            case CONFLICTING -> {
                metrics.conflictingReply.increment();
                log.error("respuesta contradictoria del rail: {} ({}) para la transferencia {} cuya saga ya terminó "
                        + "en sentido contrario (event_id {}); sin cambios de estado ni de asientos, a conciliar "
                        + "con el rail", reply.eventType(), reply.kind(), reply.transferId(), reply.eventId());
            }
            case COMPENSATION_STARTED -> railSaga.compensateNow(reply.transferId(), reply.correlationId());
            case COMPLETED, NO_CHANGE -> { }
        }
    }
}
