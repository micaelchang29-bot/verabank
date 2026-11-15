package com.verabank.transfer.web;

import java.net.URI;
import java.util.UUID;
import org.springframework.http.HttpStatusCode;
import org.springframework.http.ProblemDetail;

/** Errores RFC 9457 con los {@code type} de contracts/openapi.yaml y el correlation_id. */
public final class Problems {

    private Problems() {
    }

    public static ProblemDetail of(HttpStatusCode status, String type, String title, String detail,
            UUID correlationId) {
        ProblemDetail problem = ProblemDetail.forStatus(status);
        problem.setType(URI.create(type));
        problem.setTitle(title);
        problem.setDetail(detail);
        problem.setProperty("correlation_id",
                (correlationId != null ? correlationId : RequestContext.currentCorrelationId()).toString());
        return problem;
    }
}
