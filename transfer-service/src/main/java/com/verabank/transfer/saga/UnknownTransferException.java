package com.verabank.transfer.saga;

import java.util.UUID;

/** Respuesta del rail para una transferencia que no existe: va al DLT sin reintentos. */
public class UnknownTransferException extends RuntimeException {

    public UnknownTransferException(UUID transferId) {
        super("la transferencia " + transferId + " no existe");
    }
}
