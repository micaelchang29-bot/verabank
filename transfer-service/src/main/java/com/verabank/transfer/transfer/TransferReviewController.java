package com.verabank.transfer.transfer;

import com.verabank.transfer.web.RequestContext;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import java.util.UUID;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** Decisión del analista sobre una transferencia en {@code IN_REVIEW} (docs/saga.md §6). */
@RestController
@RequestMapping("/api/v1/transfers")
public class TransferReviewController {

    private final TransferService service;

    public TransferReviewController(TransferService service) {
        this.service = service;
    }

    @PostMapping("/{id}/review")
    public TransferView review(@PathVariable("id") UUID id, @Valid @RequestBody ReviewRequest body,
            HttpServletRequest request) {
        // La identidad del analista es la del canal que resolvió la API key (§1.3 del diseño).
        return service.review(id, body.decision(), body.comment(), RequestContext.clientId(request),
                RequestContext.correlationId(request));
    }
}
