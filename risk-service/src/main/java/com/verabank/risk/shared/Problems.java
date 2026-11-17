package com.verabank.risk.shared;

import jakarta.ws.rs.core.MediaType;
import jakarta.ws.rs.core.Response;
import java.util.LinkedHashMap;
import java.util.Map;

/** Errores RFC 9457 con los {@code type} de contracts/openapi.yaml y el correlation_id. */
public final class Problems {

    public static final String MEDIA_TYPE = "application/problem+json";

    private Problems() {
    }

    public static Response response(int status, String type, String title, String detail, String correlationId) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("type", type);
        body.put("title", title);
        body.put("status", status);
        body.put("detail", detail);
        body.put("correlation_id", correlationId);
        return Response.status(status).type(MediaType.valueOf(MEDIA_TYPE)).entity(body).build();
    }
}
