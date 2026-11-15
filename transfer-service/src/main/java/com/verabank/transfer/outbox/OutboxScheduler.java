package com.verabank.transfer.outbox;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/** Sondeo del outbox cada {@code verabank.outbox.poll-interval-ms} (100 ms, ADR-02). */
@Component
@ConditionalOnProperty(prefix = "verabank.outbox", name = "relay-enabled", havingValue = "true", matchIfMissing = true)
public class OutboxScheduler {

    private static final Logger log = LoggerFactory.getLogger(OutboxScheduler.class);

    private final OutboxRelay relay;

    public OutboxScheduler(OutboxRelay relay) {
        this.relay = relay;
    }

    @Scheduled(fixedDelayString = "${verabank.outbox.poll-interval-ms:100}")
    public void poll() {
        try {
            relay.drain();
        } catch (RuntimeException e) {
            // Base de datos caída u otro error transitorio: el próximo sondeo lo reintenta.
            log.warn("sondeo del outbox fallido: {}", e.getMessage());
        }
    }
}
