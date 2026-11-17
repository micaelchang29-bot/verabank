package com.verabank.transfer.saga;

import static org.assertj.core.api.Assertions.assertThat;

import com.verabank.transfer.support.Bank;
import com.verabank.transfer.support.Http;
import com.verabank.transfer.support.Infra;
import com.verabank.transfer.support.IntegrationTest;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * El sondeo no espera a las sagas que otra transacción ya tiene bloqueadas (docs/saga.md §7:
 * {@code FOR UPDATE SKIP LOCKED}). Con el sondeo apagado, otra conexión retiene el bloqueo de la saga
 * vencida más antigua: {@code nextDue} debe volver enseguida y saltársela, no quedarse esperando.
 */
class SagaNextDueIT extends IntegrationTest {

    @Autowired
    SagaRepository sagas;

    @Autowired
    PlatformTransactionManager txManager;

    @Test
    void nextDueNoEsperaALaSagaBloqueadaPorOtraTransaccionYLaSaltaHastaQueSeLibera() throws Exception {
        Bank bank = new Bank(jdbc, txManager);
        UUID source = bank.account("500.00");
        Http.Response r = new Http(port).postTransfer(KEY_WEB, UUID.randomUUID(), Bank.external(source, "25.00"));
        assertThat(r.status()).isEqualTo(201);
        UUID id = UUID.fromString(r.json().path("transfer_id").asText());
        // La más antigua de todas, para que sea la primera candidata del ORDER BY.
        jdbc.sql("UPDATE core.saga_state SET wake_at = now() - interval '100 years' WHERE saga_id = :t")
                .param("t", id).update();
        List<String> statuses = List.of(SagaStates.RUNNING, SagaStates.WAITING_REPLY, SagaStates.COMPENSATING);
        TransactionTemplate tx = new TransactionTemplate(txManager);
        assertThat(tx.execute(s -> sagas.nextDue(statuses)).orElseThrow().sagaId()).as("sin bloqueo, es la primera")
                .isEqualTo(id);

        try (Connection locker = DriverManager.getConnection(Infra.POSTGRES.getJdbcUrl(), Infra.TRANSFER_USER,
                Infra.TRANSFER_PASSWORD)) {
            locker.setAutoCommit(false);
            try (PreparedStatement ps = locker.prepareStatement(
                    "SELECT 1 FROM core.saga_state WHERE saga_id = ? FOR UPDATE")) {
                ps.setObject(1, id);
                ps.executeQuery();
            }

            CompletableFuture<Optional<SagaRow>> call = CompletableFuture.supplyAsync(
                    () -> tx.execute(s -> sagas.nextDue(statuses)));
            Optional<SagaRow> picked;
            try {
                picked = call.get(5, TimeUnit.SECONDS);
            } catch (TimeoutException e) {
                locker.rollback();   // libera al que esperaba para no dejar el hilo colgado
                call.get(10, TimeUnit.SECONDS);
                throw new AssertionError("nextDue esperó al bloqueo de otra transacción (falta SKIP LOCKED)", e);
            }
            assertThat(picked.map(SagaRow::sagaId)).as("la saga bloqueada se salta").isNotEqualTo(Optional.of(id));
            locker.rollback();
        }

        assertThat(tx.execute(s -> sagas.nextDue(statuses)).orElseThrow().sagaId()).as("liberada, vuelve a ser la primera")
                .isEqualTo(id);
    }
}
