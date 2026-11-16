package com.verabank.transfer.transfer;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.argThat;
import static org.mockito.Mockito.doThrow;

import com.verabank.transfer.account.AccountRepository;
import com.verabank.transfer.support.Bank;
import com.verabank.transfer.support.Http;
import com.verabank.transfer.support.IntegrationTest;
import java.math.BigDecimal;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.test.annotation.DirtiesContext;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;
import org.springframework.transaction.PlatformTransactionManager;

/**
 * Una violación de invariante en tx2 no es un fallo de datos: sale como 500 internal-error,
 * no como 503 reintentable. tx2 se revierte entera: la retención sigue ligada a la PENDING.
 */
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
class InvariantFailureIT extends IntegrationTest {

    private static final BigDecimal TRIGGER = new BigDecimal("13.13");

    @MockitoSpyBean
    AccountRepository accounts;

    @Autowired
    PlatformTransactionManager txManager;

    @Test
    void unaViolacionDeInvarianteEnTx2Es500InternalErrorNoUn503() {
        doThrow(new IllegalStateException("invariante rota"))
                .when(accounts).debitHeld(any(), argThat(a -> a != null && a.compareTo(TRIGGER) == 0));
        Bank bank = new Bank(jdbc, txManager);
        Http http = new Http(port);
        UUID source = bank.account("100.00");
        UUID dest = bank.account("0");
        UUID key = UUID.randomUUID();

        Http.Response r = http.postTransfer(KEY_WEB, key, Bank.internal(source, dest, TRIGGER.toPlainString()));

        assertThat(r.status()).isEqualTo(500);
        assertThat(r.header("Content-Type")).startsWith("application/problem+json");
        assertThat(r.json().path("type").asText()).isEqualTo("internal-error");
        assertThat(com.verabank.transfer.support.Contracts.openApiErrors("Problem", r.json())).isEmpty();
        assertThat(r.json().path("detail").asText()).doesNotContain("invariante rota");
        assertThat(r.json().path("correlation_id").asText()).isNotBlank();
        // tx2 se revirtió: sin asientos, saldo intacto, retención ligada a la transferencia PENDING.
        assertThat(bank.balance(source)).isEqualByComparingTo("100.00");
        assertThat(bank.held(source)).isEqualByComparingTo("13.13");
        assertThat(jdbc.sql("SELECT status FROM core.transfers WHERE client_id = :c AND idempotency_key = :k")
                .param("c", CLIENT_WEB).param("k", key).query(String.class).single()).isEqualTo("PENDING");
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }
}
