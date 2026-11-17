package com.verabank.risk.stream;

/**
 * Rol {@code stream}: consumidores de Kafka ({@code messaging}), cadena de reglas AML y alertas ({@code aml}), etiquetas
 * de fraude ({@code labels}) y API de alertas ({@code api}). No importa nada de {@code scoring} (ADR-08, D3): la prueba
 * de arquitectura lo vigila.
 */
public final class StreamRole {

    public static final String NAME = "stream";

    private StreamRole() {
    }
}
