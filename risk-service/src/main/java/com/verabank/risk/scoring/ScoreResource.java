package com.verabank.risk.scoring;

import com.verabank.risk.shared.ApiException;
import io.smallrye.mutiny.Uni;
import jakarta.enterprise.inject.Instance;
import jakarta.inject.Inject;
import jakarta.ws.rs.Consumes;
import jakarta.ws.rs.POST;
import jakarta.ws.rs.Path;
import jakarta.ws.rs.Produces;
import jakarta.ws.rs.core.MediaType;

/**
 * POST /internal/v1/scores (solo red interna, sin API key). Con {@code VERABANK_ROLE=stream} el servicio de scoring
 * no se activa y la ruta responde 404.
 */
@Path("/internal/v1/scores")
public class ScoreResource {

    @Inject
    Instance<ScoringService> service;

    @POST
    @Consumes(MediaType.APPLICATION_JSON)
    @Produces(MediaType.APPLICATION_JSON)
    public Uni<ScoreResponse> score(String body) {
        if (!service.isResolvable()) {
            throw ApiException.notFound("Este despliegue no tiene el rol scoring");
        }
        return service.get().handle(body).map(o -> new ScoreResponse(o.score(), o.decision(), o.modelVersion()));
    }
}
