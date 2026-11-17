package com.verabank.risk.stream.api;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.verabank.risk.shared.ApiException;
import com.verabank.risk.shared.CorrelationId;
import com.verabank.risk.shared.RiskConfig;
import io.smallrye.mutiny.Uni;
import jakarta.inject.Inject;
import jakarta.ws.rs.Consumes;
import jakarta.ws.rs.GET;
import jakarta.ws.rs.PATCH;
import jakarta.ws.rs.Path;
import jakarta.ws.rs.PathParam;
import jakarta.ws.rs.Produces;
import jakarta.ws.rs.QueryParam;
import jakarta.ws.rs.container.ContainerRequestContext;
import jakarta.ws.rs.core.Context;
import jakarta.ws.rs.core.MediaType;
import java.util.UUID;

/**
 * {@code GET /api/v1/aml/alerts} y {@code PATCH /api/v1/aml/alerts/{id}} (contracts/openapi.yaml). Con otro rol que
 * stream las rutas responden 404; la API key la exige {@link ApiKeyFilter}.
 */
@Path("/api/v1/aml/alerts")
@Produces(MediaType.APPLICATION_JSON)
public class AlertsResource {

    @Inject
    AlertService service;
    @Inject
    RiskConfig config;
    @Inject
    ObjectMapper mapper;

    @GET
    public Uni<AlertPage> list(@QueryParam("status") String status, @QueryParam("severity") String severity,
            @QueryParam("rule_code") String ruleCode, @QueryParam("limit") String limit,
            @QueryParam("cursor") String cursor) {
        requireStreamRole();
        if (status != null && !AlertTransitions.STATUSES.contains(status)) {
            throw ApiException.validation("status inválido: " + status);
        }
        if (severity != null && !AlertTransitions.SEVERITIES.contains(severity)) {
            throw ApiException.validation("severity inválido: " + severity);
        }
        if (ruleCode != null && ruleCode.isBlank()) {
            throw ApiException.validation("rule_code vacío");
        }
        return service.list(status, severity, ruleCode, parseLimit(limit), cursor);
    }

    @PATCH
    @Path("/{id}")
    @Consumes(MediaType.APPLICATION_JSON)
    public Uni<AlertView> update(@PathParam("id") String id, String body, @Context ContainerRequestContext request) {
        requireStreamRole();
        UUID alertId;
        try {
            alertId = UUID.fromString(id);
        } catch (IllegalArgumentException e) {
            throw ApiException.validation("id inválido: debe ser un UUID");
        }
        JsonNode json;
        try {
            json = mapper.readTree(body == null ? "" : body);
        } catch (JsonProcessingException e) {
            throw ApiException.validation("el cuerpo no es JSON");
        }
        if (json == null || !json.isObject()) {
            throw ApiException.validation("el cuerpo debe ser un objeto JSON");
        }
        JsonNode status = json.get("status");
        if (status == null || !status.isTextual() || !AlertTransitions.STATUSES.contains(status.asText())) {
            throw ApiException.validation("status es obligatorio y debe ser uno de " + AlertTransitions.STATUSES);
        }
        String comment = null;
        JsonNode c = json.get("comment");
        if (c != null && !c.isNull()) {
            if (!c.isTextual() || c.asText().length() > 1000) {
                throw ApiException.validation("comment debe ser texto de hasta 1000 caracteres");
            }
            comment = c.asText();
        }
        String channel = (String) request.getProperty(ApiKeyFilter.CHANNEL);
        return service.transition(alertId, status.asText(), comment, channel,
                UUID.fromString(CorrelationId.of(request)));
    }

    private void requireStreamRole() {
        if (!"stream".equals(config.role())) {
            throw ApiException.notFound("Este despliegue no tiene el rol stream");
        }
    }

    private static int parseLimit(String limit) {
        if (limit == null) {
            return AlertService.DEFAULT_LIMIT;
        }
        try {
            int value = Integer.parseInt(limit);
            if (value >= 1 && value <= AlertService.MAX_LIMIT) {
                return value;
            }
        } catch (NumberFormatException e) {
            // se informa abajo
        }
        throw ApiException.validation("limit debe ser un entero entre 1 y " + AlertService.MAX_LIMIT);
    }
}
