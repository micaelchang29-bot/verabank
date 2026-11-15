package com.verabank.transfer.transfer;

import com.verabank.transfer.web.ApiException;
import java.util.UUID;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1/transfers")
public class TransferQueryController {

    private final TransferService service;

    public TransferQueryController(TransferService service) {
        this.service = service;
    }

    @GetMapping("/{id}")
    public TransferView get(@PathVariable("id") UUID id) {
        return service.find(id).orElseThrow(() -> new ApiException(HttpStatus.NOT_FOUND, "transfer-not-found",
                "Transferencia inexistente", "La transferencia " + id + " no existe."));
    }
}
