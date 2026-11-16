package com.verabank.transfer.saga;

import static org.assertj.core.api.Assertions.assertThat;
import static org.awaitility.Awaitility.await;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.verabank.transfer.support.Bank;
import com.verabank.transfer.support.Events;
import com.verabank.transfer.support.Http;
import com.verabank.transfer.support.IntegrationTest;
import com.verabank.transfer.support.RailDouble;
import com.verabank.transfer.support.SagaEnabled;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.UUID;
import org.assertj.core.data.Offset;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.test.context.TestPropertySource;
import org.springframework.transaction.PlatformTransactionManager;

/**
 * El rail no responde (docs/saga.md S11 a S13, §3 y §5). Tiempos de prueba: primera espera 1 s,
 * consultas y cancelaciones con base 500 ms y tope 2 s, cancelación tras 3 consultas. Solo una
 * respuesta explícita del rail compensa: nunca un silencio.
 */
@SagaEnabled
@TestPropertySource(properties = {"verabank.saga.rail-reply-timeout=1s", "verabank.saga.cancel-after-polls=3"})
class RailTimeoutIT extends IntegrationTest {

    static RailDouble rail;

    @Autowired
    PlatformTransactionManager txManager;

    @Autowired
    ObjectMapper mapper;

    private Http http;
    private Bank bank;
    private UUID source;

    @BeforeAll
    static void startRail() {
        rail = RailDouble.start();
    }

    @AfterAll
    static void stopRail() {
        rail.close();
    }

    @BeforeEach
    void setUp() {
        http = new Http(port);
        bank = new Bank(jdbc, txManager);
        source = bank.account("500.00");
        rail.respondWith(c -> List.of());
    }

    private UUID externalTransfer(String amount) {
        Http.Response r = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.external(source, amount));
        assertThat(r.status()).isEqualTo(201);
        return UUID.fromString(r.json().path("transfer_id").asText());
    }

    private String status(UUID id) {
        return jdbc.sql("SELECT status FROM core.transfers WHERE transfer_id = :t").param("t", id)
                .query(String.class).single();
    }

    private String saga(UUID id) {
        return jdbc.sql("SELECT status || ':' || current_step || ':' || attempt FROM core.saga_state WHERE saga_id = :t")
                .param("t", id).query(String.class).single();
    }

    private List<String> railCommands(UUID id) {
        return rail.commandsFor(id).stream().map(RailDouble.Command::type).toList();
    }

    @Test
    void t07SinRespuestaSeConsultaYSiElRailConfirmaCompletaSinCompensarACiegas() {
        rail.respondWith(c -> c.type().equals("CreditStatusQuery") ? List.of(RailDouble.status(c.transferId(), "CONFIRMED"))
                : List.of());
        UUID id = externalTransfer("100.00");

        await().atMost(Duration.ofSeconds(20)).until(() -> "COMPLETED".equals(status(id)));

        assertThat(railCommands(id)).containsExactly("CreditInstruction", "CreditStatusQuery");
        assertThat(bank.countEntries(id, "D")).isEqualTo(1);
        assertThat(bank.countEntries(id, "C")).isEqualTo(1);
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        assertThat(bank.balance(source)).isEqualByComparingTo("400.00");
        assertThat(Events.types(jdbc, id)).doesNotContain("TransferFailed");
        assertThat(Events.countByType(jdbc, id)).containsEntry("TransferCompleted", 1L);
        assertThat(jdbc.sql("SELECT action FROM audit.audit_log WHERE entity_id = :t ORDER BY audit_id")
                .param("t", id.toString()).query(String.class).list()).doesNotContain("SAGA_COMPENSATION_STARTED");
        assertThat(bank.reconcile()).containsExactly(0, 0);
        Events.assertAllValid(jdbc, id);
    }

    @Test
    void conUnRailCalladoSeConsultaTresVecesSePideCancelarYNuncaSeCompensaSola() throws Exception {
        UUID id = externalTransfer("100.00");

        // Se observa el retardo programado tras cada consulta y cada cancelación: wake_at - updated_at.
        Map<String, Double> delays = new TreeMap<>();
        await().atMost(Duration.ofSeconds(30)).pollInterval(Duration.ofMillis(20)).until(() -> {
            var row = jdbc.sql("""
                    SELECT payload::text AS payload, EXTRACT(EPOCH FROM (wake_at - updated_at)) AS delay
                    FROM core.saga_state WHERE saga_id = :t""").param("t", id).query().singleRow();
            JsonNode payload = mapper.readTree((String) row.get("payload"));
            if (row.get("delay") != null) {
                double delay = ((Number) row.get("delay")).doubleValue();
                if (payload.has("polls")) {
                    delays.putIfAbsent("poll" + payload.get("polls").asInt(), delay);
                }
                if (payload.has("cancels")) {
                    delays.putIfAbsent("cancel" + payload.get("cancels").asInt(), delay);
                }
            }
            return payload.path("cancels").asInt() >= 4;
        });

        // base 500 ms, tope 2 s: consultas 0,5 · 1 · 2 (tras la tercera, ya tope); cancelaciones 0,5 · 1 · 2 · 2.
        assertThat(delays.get("poll1")).isCloseTo(0.5, Offset.offset(0.001));
        assertThat(delays.get("poll2")).isCloseTo(1.0, Offset.offset(0.001));
        assertThat(delays.get("poll3")).isCloseTo(2.0, Offset.offset(0.001));
        assertThat(delays.get("cancel1")).isCloseTo(0.5, Offset.offset(0.001));
        assertThat(delays.get("cancel2")).isCloseTo(1.0, Offset.offset(0.001));
        assertThat(delays.get("cancel3")).isCloseTo(2.0, Offset.offset(0.001));
        assertThat(delays.get("cancel4")).isCloseTo(2.0, Offset.offset(0.001));

        // Orden de los comandos: instrucción, tres consultas y solo entonces la cancelación (repetida).
        await().atMost(Duration.ofSeconds(10)).until(() -> rail.count(id, "CreditCancel") >= 4);
        List<String> commands = railCommands(id);
        assertThat(commands.subList(0, 5)).containsExactly("CreditInstruction", "CreditStatusQuery",
                "CreditStatusQuery", "CreditStatusQuery", "CreditCancel");
        assertThat(commands.stream().filter("CreditStatusQuery"::equals).count()).isEqualTo(3);

        // Nunca compensó: sin respuesta explícita no se libera nada.
        assertThat(status(id)).isEqualTo("PROCESSING");
        assertThat(saga(id)).startsWith("WAITING_REPLY:CANCEL_REQUESTED:0");   // el rail callado no consume attempt
        assertThat(bank.held(source)).isEqualByComparingTo("100.00");
        assertThat(Events.types(jdbc, id)).doesNotContain("TransferFailed");
        assertThat(bank.reconcile()).containsExactly(0, 0);
        Events.assertAllValid(jdbc, id);
    }

    @Test
    void unaRespuestaTardiaTrasLasConsultasYLaCancelacionTodaviaCompleta() {
        UUID id = externalTransfer("100.00");
        await().atMost(Duration.ofSeconds(30)).until(() -> rail.count(id, "CreditCancel") >= 1);
        assertThat(saga(id)).startsWith("WAITING_REPLY:CANCEL_REQUESTED");

        rail.send(RailDouble.confirmed(id));   // el rail, por fin, confirma

        await().atMost(Duration.ofSeconds(20)).until(() -> "COMPLETED".equals(status(id)));
        assertThat(saga(id)).isEqualTo("COMPLETED:DONE:0");
        assertThat(bank.countEntries(id, "D")).isEqualTo(1);
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        assertThat(Events.types(jdbc, id)).doesNotContain("TransferFailed");
        // Con la saga terminada el sondeo ya no pregunta ni cancela.
        long commands = railCommands(id).size();
        try {
            Thread.sleep(2500);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        assertThat(railCommands(id)).hasSize((int) commands);
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }

    @Test
    void unknownRepetidoNoCompensaYSoloCancelledDesencadenaLaCompensacion() {
        rail.respondWith(c -> switch (c.type()) {
            case "CreditStatusQuery" -> List.of(RailDouble.status(c.transferId(), "UNKNOWN"));
            case "CreditCancel" -> List.of(RailDouble.status(c.transferId(), "CANCELLED"));
            default -> List.of();
        });
        UUID id = externalTransfer("100.00");

        // Mientras responde UNKNOWN a cada consulta, la saga sigue esperando con su retención.
        await().atMost(Duration.ofSeconds(20)).until(() -> rail.count(id, "CreditStatusQuery") >= 3);
        assertThat(rail.count(id, "CreditCancel")).as("aún no se pidió la cancelación").isZero();
        assertThat(status(id)).isEqualTo("PROCESSING");
        assertThat(bank.held(source)).isEqualByComparingTo("100.00");

        // Tras la tercera consulta sin respuesta concluyente se pide la cancelación; CANCELLED compensa.
        await().atMost(Duration.ofSeconds(30)).until(() -> "FAILED".equals(status(id)));
        List<String> commands = railCommands(id);
        assertThat(commands).containsExactly("CreditInstruction", "CreditStatusQuery", "CreditStatusQuery",
                "CreditStatusQuery", "CreditCancel");
        assertThat(saga(id)).isEqualTo("COMPENSATED:DONE:0");
        assertThat(jdbc.sql("SELECT failure_reason FROM core.transfers WHERE transfer_id = :t").param("t", id)
                .query(String.class).single()).isEqualTo("RAIL_CANCELLED");
        assertThat(Events.outbox(jdbc, id).stream().filter(e -> e.eventType().equals("TransferFailed")).toList())
                .singleElement().satisfies(e -> {
                    assertThat(e.payload().path("reason").asText()).isEqualTo("RAIL_CANCELLED");
                    assertThat(e.payload().path("compensated").asBoolean()).isTrue();
                });
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        assertThat(bank.balance(source)).isEqualByComparingTo("500.00");
        assertThat(bank.reconcile()).containsExactly(0, 0);
        Events.assertAllValid(jdbc, id);
    }

    @Test
    void losComandosAlRailLlegaranPorKafkaConSuEsquemaYClaveTransferId() {
        UUID id = externalTransfer("100.00");
        await().atMost(Duration.ofSeconds(30)).until(() -> rail.count(id, "CreditCancel") >= 1);

        List<RailDouble.Command> commands = rail.commandsFor(id);
        assertThat(commands).extracting(RailDouble.Command::type).contains("CreditInstruction",
                "CreditStatusQuery", "CreditCancel");
        for (RailDouble.Command c : commands) {
            assertThat(c.key()).as("clave transfer_id").isEqualTo(id.toString());
            assertThat(c.envelope().path("aggregate_id").asText()).isEqualTo(id.toString());
            assertThat(com.verabank.transfer.support.Contracts.envelopeErrors(c.envelope()))
                    .as("sobre de %s", c.type()).isEmpty();
            assertThat(com.verabank.transfer.support.Contracts.payloadErrors(c.type(), c.envelope().path("payload")))
                    .as("payload de %s", c.type()).isEmpty();
        }
    }
}
