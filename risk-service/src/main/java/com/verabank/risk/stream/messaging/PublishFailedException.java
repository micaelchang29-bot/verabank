package com.verabank.risk.stream.messaging;

/** La transacción local ya se confirmó pero no se pudo publicar en Kafka: reintentar el mensaje no lo arreglaría. */
public class PublishFailedException extends RuntimeException {

    public PublishFailedException(String message, Throwable cause) {
        super(message, cause);
    }
}
