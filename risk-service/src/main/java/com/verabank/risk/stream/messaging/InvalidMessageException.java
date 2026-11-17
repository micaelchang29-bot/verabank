package com.verabank.risk.stream.messaging;

/** El mensaje no cumple el envoltorio o el esquema del payload: va directo al {@code .dlt}, sin reintentos. */
public class InvalidMessageException extends RuntimeException {

    public InvalidMessageException(String message) {
        super(message);
    }
}
