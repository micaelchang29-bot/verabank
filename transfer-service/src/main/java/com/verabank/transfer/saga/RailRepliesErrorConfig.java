package com.verabank.transfer.saga;

import com.verabank.transfer.config.VerabankProperties;
import org.apache.kafka.common.TopicPartition;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.listener.CommonErrorHandler;
import org.springframework.kafka.listener.ConsumerRecordRecoverer;
import org.springframework.kafka.listener.DeadLetterPublishingRecoverer;
import org.springframework.kafka.listener.DefaultErrorHandler;
import org.springframework.util.backoff.FixedBackOff;

/**
 * Reintentos y DLT de rail.replies.v1: tres reintentos y luego {@code rail.replies.v1.dlt}. Ese tópico
 * tiene UNA partición (§3.1), así que el destino se resuelve explícitamente a la partición 0: el
 * resolutor por defecto conservaría la partición del mensaje original (0 a 2) y fallaría.
 */
@Configuration
@ConditionalOnProperty(prefix = "verabank.rail", name = "replies-enabled", havingValue = "true",
        matchIfMissing = true)
public class RailRepliesErrorConfig {

    private static final Logger log = LoggerFactory.getLogger(RailRepliesErrorConfig.class);

    @Bean
    CommonErrorHandler railRepliesErrorHandler(KafkaTemplate<String, String> template, SagaMetrics metrics,
            VerabankProperties properties) {
        DeadLetterPublishingRecoverer dlt = new DeadLetterPublishingRecoverer(template,
                (record, error) -> new TopicPartition(record.topic() + ".dlt", 0));
        ConsumerRecordRecoverer recoverer = (record, error) -> {
            log.error("mensaje de {} (partición {}, offset {}) al DLT: {}", record.topic(), record.partition(),
                    record.offset(), rootMessage(error));
            dlt.accept(record, error);   // si el DLT falla, lanza y el mensaje se reintenta
            metrics.inboxDlt.increment();
        };
        DefaultErrorHandler handler = new DefaultErrorHandler(recoverer,
                new FixedBackOff(properties.rail().retryInterval().toMillis(), properties.rail().retries()));
        handler.addNotRetryableExceptions(InvalidReplyException.class, UnknownTransferException.class);
        handler.setCommitRecovered(true);   // commit manual: el offset del mensaje recuperado lo confirma el manejador
        return handler;
    }

    private static String rootMessage(Throwable error) {
        Throwable t = error;
        while (t.getCause() != null && t.getCause() != t) {
            t = t.getCause();
        }
        return t.getClass().getSimpleName() + ": " + t.getMessage();
    }
}
