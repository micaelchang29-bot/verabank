package com.verabank.transfer.saga;

import java.util.UUID;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

/** Inbox de core.processed_messages: un mensaje se procesa una sola vez por consumidor. */
@Repository
public class InboxRepository {

    private final JdbcClient jdbc;

    public InboxRepository(JdbcClient jdbc) {
        this.jdbc = jdbc;
    }

    /**
     * Registra el mensaje en la misma transacción que su efecto. {@code false} si ya estaba: se
     * procesó antes y no debe tener efectos (si otra transacción lo está insertando, espera a que
     * termine y también devuelve {@code false}).
     */
    public boolean markProcessed(String consumer, UUID eventId) {
        return jdbc.sql("""
                INSERT INTO core.processed_messages (consumer, event_id) VALUES (:consumer, :eventId)
                ON CONFLICT (consumer, event_id) DO NOTHING""")
                .param("consumer", consumer)
                .param("eventId", eventId)
                .update() == 1;
    }
}
