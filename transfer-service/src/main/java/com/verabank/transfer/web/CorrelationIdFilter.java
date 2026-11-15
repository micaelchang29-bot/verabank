package com.verabank.transfer.web;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * Toma el correlation_id de la cabecera {@code X-Correlation-Id} o genera uno. Viaja al MDC
 * (logs), a la respuesta, al outbox y a la auditoría. Los contratos lo exigen UUID: un valor
 * que no lo sea se reemplaza por uno nuevo.
 */
@Component
@Order(Ordered.HIGHEST_PRECEDENCE)
public class CorrelationIdFilter extends OncePerRequestFilter {

    private static final Logger log = LoggerFactory.getLogger(CorrelationIdFilter.class);

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        String header = request.getHeader(RequestContext.CORRELATION_HEADER);
        UUID correlationId = parse(header);
        boolean replaced = header != null && correlationId == null;
        if (correlationId == null) {
            correlationId = UUID.randomUUID();
        }
        request.setAttribute(RequestContext.ATTR_CORRELATION, correlationId);
        response.setHeader(RequestContext.CORRELATION_HEADER, correlationId.toString());
        MDC.put(RequestContext.MDC_CORRELATION, correlationId.toString());
        try {
            if (replaced) {
                log.info("cabecera {} no es un UUID; se usa uno generado", RequestContext.CORRELATION_HEADER);
            }
            chain.doFilter(request, response);
        } finally {
            MDC.remove(RequestContext.MDC_CORRELATION);
            MDC.remove(RequestContext.MDC_CLIENT);
        }
    }

    private static UUID parse(String value) {
        if (value == null || value.length() != 36) {
            return null;
        }
        try {
            return UUID.fromString(value);
        } catch (IllegalArgumentException e) {
            return null;
        }
    }
}
