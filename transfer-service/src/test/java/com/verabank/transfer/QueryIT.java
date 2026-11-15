package com.verabank.transfer;

import static org.assertj.core.api.Assertions.assertThat;

import com.verabank.transfer.support.Bank;
import com.verabank.transfer.support.Contracts;
import com.verabank.transfer.support.Http;
import com.verabank.transfer.support.IntegrationTest;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.transaction.PlatformTransactionManager;

/** GET de transferencias y cuentas, con las respuestas validadas contra contracts/openapi.yaml. */
class QueryIT extends IntegrationTest {

    @Autowired
    PlatformTransactionManager txManager;

    private Http http;
    private Bank bank;

    @BeforeEach
    void setUp() {
        http = new Http(port);
        bank = new Bank(jdbc, txManager);
    }

    @Test
    void getTransferDevuelveElEstadoActual() {
        UUID source = bank.account("100.00");
        UUID dest = bank.account("0");
        Http.Response created = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(source, dest, "25.00"));
        String id = created.json().path("transfer_id").asText();
        assertThat(created.header("Location")).endsWith("/api/v1/transfers/" + id);
        assertThat(Contracts.openApiErrors("Transfer", created.json())).isEmpty();

        Http.Response r = http.get(KEY_MOBILE, "/api/v1/transfers/" + id);

        assertThat(r.status()).isEqualTo(200);
        assertThat(r.json()).isEqualTo(created.json());
        assertThat(Contracts.openApiErrors("Transfer", r.json())).isEmpty();
    }

    @Test
    void getTransferInexistenteEs404TransferNotFound() {
        Http.Response r = http.get(KEY_WEB, "/api/v1/transfers/" + UUID.randomUUID());

        assertThat(r.status()).isEqualTo(404);
        assertThat(r.json().path("type").asText()).isEqualTo("transfer-not-found");
        assertThat(Contracts.openApiErrors("Problem", r.json())).isEmpty();
    }

    @Test
    void getTransferConIdQueNoEsUuidEs400() {
        Http.Response r = http.get(KEY_WEB, "/api/v1/transfers/abc");

        assertThat(r.status()).isEqualTo(400);
        assertThat(r.json().path("type").asText()).isEqualTo("validation-error");
    }

    @Test
    void getAccountDevuelveSaldoRetenidoYDisponible() {
        UUID source = bank.account("100.00");
        String external = """
                {"source_account_id":"%s","dest_external_ref":"CCI-1","dest_country":"PE","rail":"EXTERNAL",
                 "transfer_type":"TRANSFER","amount":"40.00","currency":"PEN","occurred_at":"2026-03-01T10:15:30Z"}"""
                .formatted(source);
        assertThat(http.postTransfer(KEY_WEB, UUID.randomUUID(), external).status()).isEqualTo(201);

        Http.Response r = http.get(KEY_WEB, "/api/v1/accounts/" + source);

        assertThat(r.status()).isEqualTo(200);
        assertThat(r.json().path("account_id").asText()).isEqualTo(source.toString());
        assertThat(r.json().path("status").asText()).isEqualTo("ACTIVE");
        assertThat(r.json().path("currency").asText()).isEqualTo("PEN");
        assertThat(r.json().path("balance").asText()).isEqualTo("100.00");
        assertThat(r.json().path("held").asText()).isEqualTo("40.00");
        assertThat(r.json().path("available").asText()).isEqualTo("60.00");
        assertThat(Contracts.openApiErrors("Account", r.json())).isEmpty();
    }

    @Test
    void getAccountInexistenteOCompensacionEs404AccountNotFound() {
        Http.Response missing = http.get(KEY_WEB, "/api/v1/accounts/" + UUID.randomUUID());
        Http.Response clearing = http.get(KEY_WEB, "/api/v1/accounts/" + Bank.CLEARING);

        assertThat(missing.status()).isEqualTo(404);
        assertThat(missing.json().path("type").asText()).isEqualTo("account-not-found");
        assertThat(clearing.status()).isEqualTo(404);
        assertThat(Contracts.openApiErrors("Problem", missing.json())).isEmpty();
    }

    @Test
    void elValidadorDeContratosDetectaRespuestasQueNoCumplen() {
        UUID source = bank.account("1.00");
        Http.Response r = http.get(KEY_WEB, "/api/v1/accounts/" + source);
        var broken = r.json().deepCopy();
        ((com.fasterxml.jackson.databind.node.ObjectNode) broken).put("balance", 1.0);

        assertThat(Contracts.openApiErrors("Account", broken)).isNotEmpty();
    }
}
