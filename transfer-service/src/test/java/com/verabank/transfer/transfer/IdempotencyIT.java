package com.verabank.transfer.transfer;

import static org.assertj.core.api.Assertions.assertThat;

import com.verabank.transfer.support.Bank;
import com.verabank.transfer.support.Http;
import com.verabank.transfer.support.IntegrationTest;
import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.function.Function;
import java.util.stream.Collectors;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.transaction.PlatformTransactionManager;

/** T-01, T-02 y T-03 de §6. */
class IdempotencyIT extends IntegrationTest {

    @Autowired
    PlatformTransactionManager txManager;

    private Http http;
    private Bank bank;

    @BeforeEach
    void setUp() {
        http = new Http(port);
        bank = new Bank(jdbc, txManager);
    }

    /** T-01: segunda respuesta 200 con Idempotent-Replayed; una fila, una retención. */
    @Test
    void t01DuplicadaSecuencialConLaMismaClave() {
        UUID source = bank.account("1000.00");
        UUID dest = bank.account("0");
        UUID key = UUID.randomUUID();
        String body = Bank.internal(source, dest, "150.00");

        Http.Response first = http.postTransfer(KEY_WEB, key, body);
        Http.Response second = http.postTransfer(KEY_WEB, key, body);

        assertThat(first.status()).isEqualTo(201);
        assertThat(first.header("Idempotent-Replayed")).isNull();
        assertThat(second.status()).isEqualTo(200);
        assertThat(second.header("Idempotent-Replayed")).isEqualTo("true");
        assertThat(second.json().path("transfer_id").asText()).isEqualTo(first.json().path("transfer_id").asText());

        UUID transferId = UUID.fromString(first.json().path("transfer_id").asText());
        assertThat(bank.countTransfers(CLIENT_WEB, key)).isEqualTo(1);
        assertThat(bank.countEntries(transferId, "D")).isEqualTo(1);
        assertThat(bank.countEntriesForAccount(source, "D")).isEqualTo(1);
        assertThat(bank.balance(source)).isEqualByComparingTo("850.00");
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        assertThat(heldEvents(transferId)).as("una sola retención: un TransferInitiated").isEqualTo(1);
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }

    /** T-02: un 201; el resto 200 o 409; un solo débito. */
    @Test
    void t02MismaClaveDesde20HilosALaVez() throws Exception {
        UUID source = bank.account("1000.00");
        UUID dest = bank.account("0");
        UUID key = UUID.randomUUID();
        String body = Bank.internal(source, dest, "100.00");
        int threads = 20;

        CountDownLatch start = new CountDownLatch(1);
        List<Future<Http.Response>> futures = new ArrayList<>();
        try (ExecutorService pool = Executors.newFixedThreadPool(threads)) {
            for (int i = 0; i < threads; i++) {
                futures.add(pool.submit(() -> {
                    start.await();
                    return http.postTransfer(KEY_WEB, key, body);
                }));
            }
            start.countDown();
            List<Http.Response> responses = new ArrayList<>();
            for (Future<Http.Response> f : futures) {
                responses.add(f.get());
            }

            Map<Integer, Long> byStatus = responses.stream()
                    .collect(Collectors.groupingBy(Http.Response::status, Collectors.counting()));
            assertThat(byStatus.get(201)).as("respuestas por estado: %s", byStatus).isEqualTo(1L);
            assertThat(byStatus.keySet()).as("respuestas por estado: %s", byStatus).allMatch(s -> s == 201 || s == 200 || s == 409);
            responses.stream().filter(r -> r.status() == 200)
                    .forEach(r -> assertThat(r.header("Idempotent-Replayed")).isEqualTo("true"));
            responses.stream().filter(r -> r.status() == 409).forEach(r -> {
                assertThat(r.json().path("type").asText()).isEqualTo("idempotency-in-progress");
                assertThat(r.header("Retry-After")).isNotNull();
            });
        }

        assertThat(bank.countTransfers(CLIENT_WEB, key)).isEqualTo(1);
        assertThat(bank.countEntriesForAccount(source, "D")).as("un solo débito").isEqualTo(1);
        assertThat(bank.balance(source)).isEqualByComparingTo("900.00");
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }

    /** T-03: 422 idempotency-key-reuse; sin efectos. */
    @Test
    void t03MismaClaveConCuerpoDistinto() {
        UUID source = bank.account("1000.00");
        UUID dest = bank.account("0");
        UUID key = UUID.randomUUID();
        Http.Response first = http.postTransfer(KEY_WEB, key, Bank.internal(source, dest, "100.00"));
        assertThat(first.status()).isEqualTo(201);
        BigDecimal balanceBefore = bank.balance(source);
        long auditBefore = countAudit();
        long outboxBefore = countOutbox();

        Http.Response reuse = http.postTransfer(KEY_WEB, key, Bank.internal(source, dest, "200.00"));

        assertThat(reuse.status()).isEqualTo(422);
        assertThat(reuse.header("Content-Type")).startsWith("application/problem+json");
        assertThat(reuse.json().path("type").asText()).isEqualTo("idempotency-key-reuse");
        assertThat(reuse.json().path("correlation_id").asText()).isNotBlank();
        assertThat(bank.countTransfers(CLIENT_WEB, key)).isEqualTo(1);
        assertThat(bank.balance(source)).isEqualByComparingTo(balanceBefore);
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        assertThat(bank.countEntriesForAccount(source, "D")).isEqualTo(1);
        assertThat(countAudit()).as("sin auditoría nueva").isEqualTo(auditBefore);
        assertThat(countOutbox()).as("sin eventos nuevos").isEqualTo(outboxBefore);
    }

    @Test
    void elMismoCuerpoConOtroFormatoNoEsUnCuerpoDistinto() {
        UUID source = bank.account("1000.00");
        UUID dest = bank.account("0");
        UUID key = UUID.randomUUID();
        assertThat(http.postTransfer(KEY_WEB, key, Bank.internal(source, dest, "100.00")).status()).isEqualTo(201);

        String sameMeaning = """
                {"currency":"PEN","amount":"100","occurred_at":"2026-03-01T05:15:30-05:00",
                 "transfer_type":"TRANSFER","rail":"INTERNAL","dest_country":"PE",
                 "dest_account_id":"%s","source_account_id":"%s"}""".formatted(dest, source);
        Http.Response replay = http.postTransfer(KEY_WEB, key, sameMeaning);

        assertThat(replay.status()).isEqualTo(200);
        assertThat(replay.header("Idempotent-Replayed")).isEqualTo("true");
    }

    @Test
    void laClaveEsPorCanalOtroCanalConLaMismaClaveCreaOtraTransferencia() {
        UUID source = bank.account("1000.00");
        UUID dest = bank.account("0");
        UUID key = UUID.randomUUID();
        String body = Bank.internal(source, dest, "100.00");

        assertThat(http.postTransfer(KEY_WEB, key, body).status()).isEqualTo(201);
        assertThat(http.postTransfer(KEY_MOBILE, key, body).status()).isEqualTo(201);

        assertThat(bank.countTransfers(CLIENT_WEB, key)).isEqualTo(1);
        assertThat(bank.countTransfers(CLIENT_MOBILE, key)).isEqualTo(1);
        assertThat(bank.balance(source)).isEqualByComparingTo("800.00");
    }

    @Test
    void sinIdempotencyKeyEs400() {
        UUID source = bank.account("100.00");
        UUID dest = bank.account("0");

        Http.Response missing = http.postTransfer(KEY_WEB, null, Bank.internal(source, dest, "1.00"));
        Http.Response notUuid = http.send("POST", "/api/v1/transfers",
                Map.of("X-API-Key", KEY_WEB, "Idempotency-Key", "abc"),
                Bank.internal(source, dest, "1.00"));

        Function<Http.Response, String> type = r -> r.json().path("type").asText();
        assertThat(missing.status()).isEqualTo(400);
        assertThat(type.apply(missing)).isEqualTo("validation-error");
        assertThat(notUuid.status()).isEqualTo(400);
        assertThat(type.apply(notUuid)).isEqualTo("validation-error");
        assertThat(bank.balance(source)).isEqualByComparingTo("100.00");
    }

    private int heldEvents(UUID transferId) {
        return jdbc.sql("SELECT count(*) FROM core.outbox WHERE aggregate_id = :id AND event_type = 'TransferInitiated'")
                .param("id", transferId).query(Integer.class).single();
    }

    private long countAudit() {
        return jdbc.sql("SELECT count(*) FROM audit.audit_log").query(Long.class).single();
    }

    private long countOutbox() {
        return jdbc.sql("SELECT count(*) FROM core.outbox").query(Long.class).single();
    }
}
