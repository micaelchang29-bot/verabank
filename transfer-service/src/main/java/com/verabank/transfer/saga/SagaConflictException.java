package com.verabank.transfer.saga;

/**
 * La versión de la saga ya no es la leída: otro actor la transitó. La transacción se revierte entera;
 * quien llama relee la saga y decide de nuevo, o descarta el trabajo si perdió una reclamación.
 */
public class SagaConflictException extends RuntimeException {

    public SagaConflictException(String message) {
        super(message, null, false, false);
    }
}
