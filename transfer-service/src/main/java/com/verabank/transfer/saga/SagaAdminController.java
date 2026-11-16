package com.verabank.transfer.saga;

import com.verabank.transfer.web.RequestContext;
import jakarta.servlet.http.HttpServletRequest;
import java.util.UUID;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** Operación interna sobre sagas (solo red interna, con API key). */
@RestController
@RequestMapping("/internal/v1/sagas")
public class SagaAdminController {

    private final SagaAdminService service;

    public SagaAdminController(SagaAdminService service) {
        this.service = service;
    }

    @PostMapping("/{id}/resume")
    public SagaAdminService.SagaView resume(@PathVariable("id") UUID id, HttpServletRequest request) {
        return service.resume(id, RequestContext.clientId(request), RequestContext.correlationId(request));
    }
}
