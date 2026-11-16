package com.verabank.transfer.saga;

import com.verabank.transfer.audit.AuditWriter;
import com.verabank.transfer.web.ApiException;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * Reanudación operativa (S16, docs/saga.md §8): devuelve una saga {@code COMPENSATION_FAILED} al estado
 * en que se detuvo. Es la reanudación de lo que falló, no de la causa: si el fallo persiste, vuelve a
 * COMPENSATION_FAILED tras {@code saga.max-attempts} intentos.
 */
@Service
public class SagaAdminService {

    private static final Logger log = LoggerFactory.getLogger(SagaAdminService.class);

    /** Estado de la saga que devuelve la API. */
    public record SagaView(UUID sagaId, String status, String currentStep, int attempt, Instant wakeAt,
            long version, Instant updatedAt) {

        static SagaView of(SagaRow s) {
            return new SagaView(s.sagaId(), s.status(), s.currentStep(), s.attempt(), s.wakeAt(), s.version(),
                    s.updatedAt());
        }
    }

    private final SagaRepository sagas;
    private final AuditWriter audit;
    private final TransactionTemplate tx;

    public SagaAdminService(SagaRepository sagas, AuditWriter audit, PlatformTransactionManager txManager) {
        this.sagas = sagas;
        this.audit = audit;
        this.tx = new TransactionTemplate(txManager);
        this.tx.setIsolationLevel(TransactionDefinition.ISOLATION_READ_COMMITTED);
    }

    public SagaView resume(UUID sagaId, String actor, UUID correlationId) {
        SagaRow resumed = tx.execute(s -> {
            SagaRow saga = sagas.findForUpdate(sagaId).orElseThrow(() -> new ApiException(HttpStatus.NOT_FOUND,
                    "not-found", "Recurso inexistente", "La saga " + sagaId + " no existe."));
            if (!SagaStates.COMPENSATION_FAILED.equals(saga.status())) {
                throw new ApiException(HttpStatus.CONFLICT, "saga-not-resumable", "Saga no reanudable",
                        "La saga está en " + saga.status() + "; solo se reanuda COMPENSATION_FAILED.");
            }
            String resumeStatus = resumeStatus(saga);
            if (!sagas.resume(sagaId, saga.version(), resumeStatus)) {
                throw new SagaConflictException("la saga " + sagaId + " cambió durante la reanudación");
            }
            Map<String, Object> detail = new LinkedHashMap<>();
            detail.put("from_status", SagaStates.COMPENSATION_FAILED);
            detail.put("to_status", resumeStatus);
            detail.put("current_step", saga.currentStep());
            detail.put("last_error", saga.lastError());
            audit.record(actor, "SAGA_RESUMED", AuditWriter.ENTITY_TRANSFER, sagaId.toString(), correlationId, detail);
            return sagas.find(sagaId).orElseThrow();
        });
        log.warn("saga {} reanudada por {} en {}/{}", sagaId, actor, resumed.status(), resumed.currentStep());
        return SagaView.of(resumed);
    }

    /** El estado previo guardado al detenerla; si faltara, el que corresponde a su paso. */
    private static String resumeStatus(SagaRow saga) {
        String stored = saga.payloadText("resume_status");
        if (stored != null) {
            return stored;
        }
        return switch (saga.currentStep()) {
            case SagaStates.FUNDS_HELD -> SagaStates.RUNNING;
            case SagaStates.CREDIT_REQUESTED, SagaStates.CANCEL_REQUESTED -> SagaStates.WAITING_REPLY;
            default -> SagaStates.COMPENSATING;
        };
    }
}
