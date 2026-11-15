package com.verabank.transfer.transfer;

import com.verabank.transfer.web.ApiException;
import com.verabank.transfer.web.RequestContext;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import java.util.UUID;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1/transfers")
public class TransferController {

    public static final String IDEMPOTENCY_HEADER = "Idempotency-Key";
    public static final String REPLAYED_HEADER = "Idempotent-Replayed";

    private final TransferService service;

    public TransferController(TransferService service) {
        this.service = service;
    }

    @PostMapping
    public ResponseEntity<TransferView> create(
            @RequestHeader(name = IDEMPOTENCY_HEADER, required = false) String idempotencyKey,
            @Valid @RequestBody TransferRequest body,
            HttpServletRequest request) {
        UUID key = parseKey(idempotencyKey);
        TransferService.Outcome outcome = service.create(RequestContext.clientId(request), key,
                NewTransfer.from(body), RequestContext.correlationId(request));
        if (outcome.replayed()) {
            return ResponseEntity.ok().header(REPLAYED_HEADER, "true").body(outcome.transfer());
        }
        return ResponseEntity.status(HttpStatus.CREATED).body(outcome.transfer());
    }

    private static UUID parseKey(String value) {
        if (value == null || value.isBlank()) {
            throw ApiException.validation("Falta la cabecera " + IDEMPOTENCY_HEADER + ".");
        }
        try {
            if (value.length() != 36) {
                throw new IllegalArgumentException();
            }
            return UUID.fromString(value);
        } catch (IllegalArgumentException e) {
            throw ApiException.validation("La cabecera " + IDEMPOTENCY_HEADER + " debe ser un UUID.");
        }
    }
}
