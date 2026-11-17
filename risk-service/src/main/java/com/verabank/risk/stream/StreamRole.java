package com.verabank.risk.stream;

/**
 * Marcador del rol {@code stream} (consumidor de Kafka y reglas AML, tarea 4.E). Todavía no tiene lógica: este
 * paquete existe para que el límite con {@code scoring} (ADR-08) ya esté vigilado por la prueba de arquitectura.
 */
public final class StreamRole {

    public static final String NAME = "stream";

    private StreamRole() {
    }
}
