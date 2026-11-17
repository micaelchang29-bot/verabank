package com.verabank.risk.stream;

import static com.verabank.risk.stream.StreamSupport.awaitProcessed;
import static com.verabank.risk.stream.StreamSupport.counter;
import static com.verabank.risk.stream.StreamSupport.deadLetters;
import static org.assertj.core.api.Assertions.assertThat;

import com.verabank.risk.support.Db;
import com.verabank.risk.support.KafkaInfra;
import io.micrometer.core.instrument.MeterRegistry;
import io.quarkus.test.junit.QuarkusTest;
import io.quarkus.test.junit.TestProfile;
import jakarta.inject.Inject;
import java.time.Duration;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import org.awaitility.Awaitility;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/** fraud.labels.v1 → risk.fraud_labels, con inbox. */
@QuarkusTest
@TestProfile(StreamProfile.class)
class FraudLabelsIT {

    @Inject
    MeterRegistry registry;

    @BeforeEach
    void reset() {
        StreamDb.reset();
    }

    private static void send(UUID transferId, String json) {
        KafkaInfra.send(KafkaInfra.FRAUD_LABELS, transferId.toString(), json);
    }

    @Test
    void aLabelIsStoredAndARepeatedLabelWithAnotherValueUpdatesIsFraudAndLabeledAt() throws Exception {
        UUID transferId = UUID.randomUUID();
        UUID first = UUID.randomUUID();
        send(transferId, Events.fraudLabel(first, transferId, false, "SIMULATOR"));
        awaitProcessed(first);
        assertThat(Db.queryString("SELECT is_fraud::text FROM risk.fraud_labels WHERE transfer_id = ?", transferId)).isEqualTo("false");
        assertThat(Db.queryString("SELECT label_source FROM risk.fraud_labels WHERE transfer_id = ?", transferId)).isEqualTo("SIMULATOR");
        String labeledAt = Db.queryString("SELECT labeled_at::text FROM risk.fraud_labels WHERE transfer_id = ?", transferId);
        assertThat(Db.queryLong("SELECT count(*) FROM risk.processed_messages WHERE consumer = 'risk-stream:fraud-labels' AND event_id = ?", first)).isEqualTo(1);

        Thread.sleep(20);
        UUID second = UUID.randomUUID();
        send(transferId, Events.fraudLabel(second, transferId, true, "CHARGEBACK"));
        awaitProcessed(second);
        assertThat(Db.queryLong("SELECT count(*) FROM risk.fraud_labels WHERE transfer_id = ?", transferId)).isEqualTo(1);
        assertThat(Db.queryString("SELECT is_fraud::text FROM risk.fraud_labels WHERE transfer_id = ?", transferId)).isEqualTo("true");
        assertThat(Db.queryString("SELECT label_source FROM risk.fraud_labels WHERE transfer_id = ?", transferId)).isEqualTo("CHARGEBACK");
        assertThat(Db.queryLong("SELECT count(*) FROM risk.fraud_labels WHERE transfer_id = ? AND labeled_at > ?::timestamptz",
                transferId, labeledAt)).isEqualTo(1);
    }

    @Test
    void aRedeliveredOlderLabelDoesNotOverwriteTheNewerOne() {
        UUID transferId = UUID.randomUUID();
        UUID older = UUID.randomUUID();
        UUID newer = UUID.randomUUID();
        String olderJson = Events.fraudLabel(older, transferId, false, "SIMULATOR");
        send(transferId, olderJson);
        send(transferId, Events.fraudLabel(newer, transferId, true, "ANALYST"));
        awaitProcessed(newer);
        double duplicates = counter(registry, "stream.inbox.duplicates", "consumer", "risk-stream:fraud-labels");
        send(transferId, olderJson);   // reentrega del primero: el inbox la descarta
        UUID marker = UUID.randomUUID();
        send(transferId, Events.fraudLabel(marker, transferId, true, "ANALYST"));
        awaitProcessed(marker);
        assertThat(Db.queryString("SELECT is_fraud::text FROM risk.fraud_labels WHERE transfer_id = ?", transferId)).isEqualTo("true");
        assertThat(counter(registry, "stream.inbox.duplicates", "consumer", "risk-stream:fraud-labels") - duplicates).isEqualTo(1);
    }

    @Test
    void theSameLabelUnderAnotherEventIdDoesNotTouchLabeledAt() throws Exception {
        UUID transferId = UUID.randomUUID();
        UUID first = UUID.randomUUID();
        send(transferId, Events.fraudLabel(first, transferId, true, "ANALYST"));
        awaitProcessed(first);
        String labeledAt = Db.queryString("SELECT labeled_at::text FROM risk.fraud_labels WHERE transfer_id = ?", transferId);
        Thread.sleep(20);
        UUID second = UUID.randomUUID();
        send(transferId, Events.fraudLabel(second, transferId, true, "ANALYST"));
        awaitProcessed(second);
        assertThat(Db.queryString("SELECT labeled_at::text FROM risk.fraud_labels WHERE transfer_id = ?", transferId)).isEqualTo(labeledAt);
    }

    @Test
    void anInvalidLabelGoesToTheDlt() {
        UUID transferId = UUID.randomUUID();
        UUID eventId = UUID.randomUUID();
        String invalid = Events.fraudLabel(eventId, transferId, true, "TELEPATHY");   // label_source fuera del enum
        send(transferId, invalid);
        Awaitility.await().atMost(30, TimeUnit.SECONDS).pollInterval(Duration.ofMillis(100))
                .until(() -> !deadLetters(KafkaInfra.FRAUD_LABELS, transferId.toString()).isEmpty());
        assertThat(Db.queryLong("SELECT count(*) FROM risk.fraud_labels WHERE transfer_id = ?", transferId)).isZero();
        assertThat(Db.queryLong("SELECT count(*) FROM risk.processed_messages WHERE event_id = ?", eventId)).isZero();
    }
}
