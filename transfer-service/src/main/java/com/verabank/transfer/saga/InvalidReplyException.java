package com.verabank.transfer.saga;

/** Mensaje de rail.replies.v1 que no se puede procesar nunca: va al DLT sin reintentos (docs/saga.md §4). */
public class InvalidReplyException extends RuntimeException {

    public InvalidReplyException(String message) {
        super(message);
    }
}
