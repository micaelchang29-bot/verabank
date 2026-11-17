package com.verabank.risk.stream.messaging;

import io.smallrye.reactive.messaging.kafka.api.IncomingKafkaRecordMetadata;
import org.apache.kafka.common.header.Headers;

/** Lo que se necesita de un registro de Kafka para procesarlo y, si falla, enviarlo al DLT. */
public record SourceRecord(String topic, int partition, long offset, String key, String value, Headers headers) {

    public static SourceRecord of(IncomingKafkaRecordMetadata<String, String> metadata, String value) {
        return new SourceRecord(metadata.getTopic(), metadata.getPartition(), metadata.getOffset(), metadata.getKey(),
                value, metadata.getHeaders());
    }
}
