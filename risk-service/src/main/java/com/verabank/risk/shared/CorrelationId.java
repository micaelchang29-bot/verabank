package com.verabank.risk.shared;

import jakarta.ws.rs.container.ContainerRequestContext;
import jakarta.ws.rs.container.ContainerResponseContext;
import java.util.UUID;
import org.jboss.resteasy.reactive.server.ServerRequestFilter;
import org.jboss.resteasy.reactive.server.ServerResponseFilter;

/**
 * Toma el correlation_id de {@code X-Correlation-Id} o genera uno, y lo devuelve en la respuesta. Los contratos lo
 * exigen UUID: un valor que no lo sea se reemplaza por uno nuevo (igual que transfer-service).
 */
public final class CorrelationId {

    public static final String HEADER = "X-Correlation-Id";
    public static final String PROPERTY = "verabank.correlation-id";

    private CorrelationId() {
    }

    public static String of(ContainerRequestContext request) {
        Object value = request.getProperty(PROPERTY);
        return value != null ? value.toString() : UUID.randomUUID().toString();
    }

    static UUID parse(String value) {
        if (value == null || value.length() != 36) {
            return null;
        }
        try {
            return UUID.fromString(value);
        } catch (IllegalArgumentException e) {
            return null;
        }
    }

    public static class Filters {

        @ServerRequestFilter(preMatching = true)
        public void assign(ContainerRequestContext request) {
            UUID id = parse(request.getHeaderString(HEADER));
            request.setProperty(PROPERTY, (id != null ? id : UUID.randomUUID()).toString());
        }

        @ServerResponseFilter
        public void echo(ContainerRequestContext request, ContainerResponseContext response) {
            response.getHeaders().putSingle(HEADER, of(request));
        }
    }
}
