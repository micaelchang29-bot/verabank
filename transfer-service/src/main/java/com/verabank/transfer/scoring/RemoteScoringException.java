package com.verabank.transfer.scoring;

/** Fallos de la llamada a risk-service. Los de {@link Rejected} no cuentan para el circuit breaker. */
class RemoteScoringException extends RuntimeException {

    RemoteScoringException(String message, Throwable cause) {
        super(message, cause);
    }

    RemoteScoringException(String message) {
        super(message);
    }

    /** 5xx de risk-service: cuenta como fallo del circuito. */
    static final class ServerError extends RemoteScoringException {
        ServerError(int status) {
            super("risk-service respondió " + status);
        }
    }

    /** Respuesta 200 que no cumple ScoreResponse: cuenta como fallo del circuito. */
    static final class Malformed extends RemoteScoringException {
        Malformed(String message, Throwable cause) {
            super(message, cause);
        }
    }

    /** 4xx: un defecto nuestro (solicitud mal formada). Se usa el fallback pero no cuenta para el circuito. */
    static final class Rejected extends RemoteScoringException {
        Rejected(int status, String body) {
            super("risk-service rechazó la solicitud con " + status + ": " + body);
        }
    }
}
