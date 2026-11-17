package com.verabank.risk.shared;

import jakarta.ws.rs.NotAllowedException;
import jakarta.ws.rs.NotFoundException;
import jakarta.ws.rs.container.ContainerRequestContext;
import jakarta.ws.rs.core.Response;
import org.jboss.logging.Logger;
import org.jboss.resteasy.reactive.server.ServerExceptionMapper;

/** Todos los errores salen como application/problem+json con correlation_id (contracts/openapi.yaml). */
public class ApiExceptionMappers {

    private static final Logger LOG = Logger.getLogger(ApiExceptionMappers.class);

    @ServerExceptionMapper
    public Response api(ApiException e, ContainerRequestContext request) {
        return Problems.response(e.status(), e.type(), e.title(), e.getMessage(), CorrelationId.of(request));
    }

    @ServerExceptionMapper
    public Response notFound(NotFoundException e, ContainerRequestContext request) {
        return Problems.response(404, "not-found", "No encontrado", "El recurso no existe", CorrelationId.of(request));
    }

    @ServerExceptionMapper
    public Response notAllowed(NotAllowedException e, ContainerRequestContext request) {
        return Problems.response(405, "method-not-allowed", "Método no permitido",
                "El método " + request.getMethod() + " no está permitido en este recurso", CorrelationId.of(request));
    }

    @ServerExceptionMapper
    public Response unexpected(Throwable e, ContainerRequestContext request) {
        String correlationId = CorrelationId.of(request);
        LOG.errorf(e, "error inesperado [correlation_id=%s]", correlationId);
        return Problems.response(500, "internal-error", "Error interno",
                "Error inesperado; consulta los logs con el correlation_id", correlationId);
    }
}
