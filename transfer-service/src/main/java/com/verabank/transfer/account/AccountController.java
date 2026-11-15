package com.verabank.transfer.account;

import com.verabank.transfer.web.ApiException;
import java.util.UUID;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1/accounts")
public class AccountController {

    private final AccountRepository accounts;

    public AccountController(AccountRepository accounts) {
        this.accounts = accounts;
    }

    /**
     * Saldo y retenciones. Solo cuentas de cliente: la de compensación (CLEARING) es interna,
     * tiene saldo negativo por diseño y no se expone a los canales.
     */
    @GetMapping("/{id}")
    public AccountView get(@PathVariable("id") UUID id) {
        return accounts.find(id)
                .filter(AccountRow::isCustomer)
                .map(AccountView::of)
                .orElseThrow(() -> new ApiException(HttpStatus.NOT_FOUND, "account-not-found", "Cuenta inexistente",
                        "La cuenta " + id + " no existe."));
    }
}
