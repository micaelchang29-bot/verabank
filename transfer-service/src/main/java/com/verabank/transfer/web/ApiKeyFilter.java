package com.verabank.transfer.web;

import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.util.Optional;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ProblemDetail;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

/** Exige {@code X-API-Key} en {@code /api/**}; sin clave válida responde 401 unauthorized. */
@Component
@Order(Ordered.HIGHEST_PRECEDENCE + 10)
public class ApiKeyFilter extends OncePerRequestFilter {

    public static final String HEADER = "X-API-Key";
    private static final Logger log = LoggerFactory.getLogger(ApiKeyFilter.class);

    private final ApiKeyRegistry registry;
    private final ObjectMapper mapper;

    public ApiKeyFilter(ApiKeyRegistry registry, ObjectMapper mapper) {
        this.registry = registry;
        this.mapper = mapper;
    }

    @Override
    protected boolean shouldNotFilter(HttpServletRequest request) {
        String path = request.getRequestURI().substring(request.getContextPath().length());
        return !(path.equals("/api") || path.startsWith("/api/"));
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        Optional<String> client = registry.resolve(request.getHeader(HEADER));
        if (client.isEmpty()) {
            // Nunca se registra el valor recibido: solo si faltaba o no era válido.
            log.warn("solicitud rechazada: API key {}", request.getHeader(HEADER) == null ? "ausente" : "inválida");
            writeUnauthorized(request, response);
            return;
        }
        request.setAttribute(RequestContext.ATTR_CLIENT, client.get());
        MDC.put(RequestContext.MDC_CLIENT, client.get());
        chain.doFilter(request, response);
    }

    private void writeUnauthorized(HttpServletRequest request, HttpServletResponse response) throws IOException {
        ProblemDetail problem = Problems.of(HttpStatus.UNAUTHORIZED, "unauthorized", "API key ausente o inválida",
                "La cabecera " + HEADER + " falta o no corresponde a ningún canal.",
                RequestContext.correlationId(request));
        response.setStatus(HttpStatus.UNAUTHORIZED.value());
        response.setContentType(MediaType.APPLICATION_PROBLEM_JSON_VALUE);
        response.setHeader("WWW-Authenticate", "ApiKey header=\"" + HEADER + "\"");
        mapper.writeValue(response.getOutputStream(), problem);
    }
}
