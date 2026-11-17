package com.verabank.risk.stream.api;

import com.verabank.risk.shared.CorrelationId;
import com.verabank.risk.shared.Problems;
import com.verabank.risk.shared.RiskConfig;
import io.quarkus.runtime.StartupEvent;
import jakarta.enterprise.event.Observes;
import jakarta.inject.Inject;
import jakarta.ws.rs.container.ContainerRequestContext;
import jakarta.ws.rs.core.Response;
import java.util.Optional;
import org.jboss.logging.Logger;
import org.jboss.resteasy.reactive.server.ServerRequestFilter;

/**
 * Exige {@code X-API-Key} en {@code /api/**} con el rol stream (contracts/openapi.yaml: {@code 401 unauthorized}).
 * Con otro rol no hace nada: la ruta no existe y responde 404.
 */
public class ApiKeyFilter {

    public static final String HEADER = "X-API-Key";
    public static final String CHANNEL = "verabank.channel";

    private static final Logger LOG = Logger.getLogger(ApiKeyFilter.class);

    @Inject
    ApiKeyRegistry registry;
    @Inject
    RiskConfig config;

    void onStart(@Observes StartupEvent event) {
        if ("stream".equals(config.role())) {
            registry.init();
        }
    }

    @ServerRequestFilter
    public Optional<Response> authenticate(ContainerRequestContext request) {
        String path = request.getUriInfo().getPath();
        boolean api = path.equals("/api") || path.startsWith("/api/");
        if (!api || !"stream".equals(config.role())) {
            return Optional.empty();
        }
        String presented = request.getHeaderString(HEADER);
        Optional<String> channel = registry.resolve(presented);
        if (channel.isEmpty()) {
            // Nunca se registra el valor recibido: solo si faltaba o no era válido.
            LOG.warnf("solicitud rechazada: API key %s", presented == null ? "ausente" : "inválida");
            Response problem = Problems.response(401, "unauthorized", "API key ausente o inválida",
                    "La cabecera " + HEADER + " falta o no corresponde a ningún canal.", CorrelationId.of(request));
            return Optional.of(Response.fromResponse(problem)
                    .header("WWW-Authenticate", "ApiKey header=\"" + HEADER + "\"").build());
        }
        request.setProperty(CHANNEL, channel.get());
        return Optional.empty();
    }
}
