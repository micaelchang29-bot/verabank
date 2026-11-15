package com.verabank.transfer.transfer;

import static org.assertj.core.api.Assertions.assertThat;

import com.verabank.transfer.support.Bank;
import com.verabank.transfer.support.Http;
import com.verabank.transfer.support.IntegrationTest;
import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicBoolean;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.RepeatedTest;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.transaction.PlatformTransactionManager;

/** T-04 de §6 y el resto de rechazos de negocio de la creación. */
class InsufficientFundsIT extends IntegrationTest {

    @Autowired
    PlatformTransactionManager txManager;

    private Http http;
    private Bank bank;

    @BeforeEach
    void setUp() {
        http = new Http(port);
        bank = new Bank(jdbc, txManager);
    }

    /** T-04: una 422 insufficient-funds; saldo disponible nunca negativo. */
    @RepeatedTest(5)
    void t04DosTransferenciasConcurrentesQueJuntasExcedenElSaldo() throws Exception {
        UUID source = bank.account("100.00");
        UUID destA = bank.account("0");
        UUID destB = bank.account("0");

        AtomicBoolean negativeSeen = new AtomicBoolean(false);
        AtomicBoolean watching = new AtomicBoolean(true);
        CountDownLatch start = new CountDownLatch(1);
        List<Http.Response> responses = new ArrayList<>();
        try (ExecutorService pool = Executors.newFixedThreadPool(3)) {
            Future<?> watcher = pool.submit(() -> {
                while (watching.get()) {
                    BigDecimal available = jdbc.sql("SELECT balance - held FROM core.accounts WHERE account_id = :id")
                            .param("id", source).query(BigDecimal.class).single();
                    if (available.signum() < 0) {
                        negativeSeen.set(true);
                    }
                }
            });
            Future<Http.Response> a = pool.submit(() -> {
                start.await();
                return http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(source, destA, "70.00"));
            });
            Future<Http.Response> b = pool.submit(() -> {
                start.await();
                return http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(source, destB, "60.00"));
            });
            start.countDown();
            responses.add(a.get());
            responses.add(b.get());
            watching.set(false);
            watcher.get();
        }

        assertThat(responses).extracting(Http.Response::status).containsExactlyInAnyOrder(201, 422);
        Http.Response rejected = responses.stream().filter(r -> r.status() == 422).findFirst().orElseThrow();
        assertThat(rejected.header("Content-Type")).startsWith("application/problem+json");
        assertThat(rejected.json().path("type").asText()).isEqualTo("insufficient-funds");

        assertThat(negativeSeen).as("saldo disponible negativo durante la carrera").isFalse();
        BigDecimal balance = bank.balance(source);
        assertThat(balance).isIn(new BigDecimal("30.00"), new BigDecimal("40.00"));
        assertThat(balance.subtract(bank.held(source))).isGreaterThanOrEqualTo(BigDecimal.ZERO);
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        assertThat(bank.balance(destA).add(bank.balance(destB))).isEqualByComparingTo(new BigDecimal("100.00").subtract(balance));
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }

    @Test
    void laSolicitudRechazadaQuedaRegistradaYSuReintentoDevuelveElEstado() {
        UUID source = bank.account("10.00");
        UUID dest = bank.account("0");
        UUID key = UUID.randomUUID();
        String body = Bank.internal(source, dest, "10.01");

        Http.Response first = http.postTransfer(KEY_WEB, key, body);
        Http.Response retry = http.postTransfer(KEY_WEB, key, body);

        assertThat(first.status()).isEqualTo(422);
        assertThat(first.json().path("type").asText()).isEqualTo("insufficient-funds");
        assertThat(bank.countTransfers(CLIENT_WEB, key)).as("ADR-03: toda solicitud válida crea una fila").isEqualTo(1);
        assertThat(retry.status()).isEqualTo(200);
        assertThat(retry.header("Idempotent-Replayed")).isEqualTo("true");
        assertThat(retry.json().path("status").asText()).isEqualTo("REJECTED");
        assertThat(retry.json().path("failure_reason").asText()).isEqualTo("INSUFFICIENT_FUNDS");
        assertThat(bank.balance(source)).isEqualByComparingTo("10.00");
        assertThat(bank.held(source)).isEqualByComparingTo("0");
    }

    @Test
    void cuentaBloqueadaEs422AccountNotActive() {
        UUID source = bank.account("100.00", "BLOCKED");
        UUID dest = bank.account("0");

        Http.Response r = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(source, dest, "10.00"));

        assertThat(r.status()).isEqualTo(422);
        assertThat(r.json().path("type").asText()).isEqualTo("account-not-active");
        assertThat(bank.held(source)).isEqualByComparingTo("0");
    }

    @Test
    void destinoCerradoEs422AccountNotActive() {
        UUID source = bank.account("100.00");
        UUID dest = bank.account("0", "CLOSED");

        Http.Response r = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(source, dest, "10.00"));

        assertThat(r.status()).isEqualTo(422);
        assertThat(r.json().path("type").asText()).isEqualTo("account-not-active");
        assertThat(bank.balance(source)).isEqualByComparingTo("100.00");
    }

    @Test
    void cuentaInexistenteEs404SinFila() {
        UUID dest = bank.account("0");
        UUID key = UUID.randomUUID();

        Http.Response r = http.postTransfer(KEY_WEB, key, Bank.internal(UUID.randomUUID(), dest, "10.00"));

        assertThat(r.status()).isEqualTo(404);
        assertThat(r.json().path("type").asText()).isEqualTo("account-not-found");
        assertThat(bank.countTransfers(CLIENT_WEB, key)).isZero();
    }

    @Test
    void laCuentaDeCompensacionNoEsVisibleParaLosCanales() {
        UUID dest = bank.account("0");

        Http.Response r = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(Bank.CLEARING, dest, "10.00"));

        assertThat(r.status()).isEqualTo(404);
        assertThat(r.json().path("type").asText()).isEqualTo("account-not-found");
    }
}
