package com.verabank.transfer.web;

import jakarta.servlet.http.HttpServletRequest;
import java.util.UUID;
import org.springframework.web.context.request.RequestAttributes;
import org.springframework.web.context.request.RequestContextHolder;

/** Datos de la solicitud en curso que fijan los filtros: correlation_id y client_id. */
public final class RequestContext {

    public static final String CORRELATION_HEADER = "X-Correlation-Id";
    public static final String MDC_CORRELATION = "correlation_id";
    public static final String MDC_CLIENT = "client_id";

    static final String ATTR_CORRELATION = RequestContext.class.getName() + ".correlationId";
    static final String ATTR_CLIENT = RequestContext.class.getName() + ".clientId";

    private RequestContext() {
    }

    public static UUID correlationId(HttpServletRequest request) {
        Object value = request.getAttribute(ATTR_CORRELATION);
        return value instanceof UUID id ? id : null;
    }

    public static String clientId(HttpServletRequest request) {
        Object value = request.getAttribute(ATTR_CLIENT);
        return value instanceof String s ? s : null;
    }

    /** correlation_id de la solicitud ligada al hilo actual, o uno nuevo si no hay solicitud. */
    public static UUID currentCorrelationId() {
        RequestAttributes attrs = RequestContextHolder.getRequestAttributes();
        if (attrs != null
                && attrs.getAttribute(ATTR_CORRELATION, RequestAttributes.SCOPE_REQUEST) instanceof UUID id) {
            return id;
        }
        return UUID.randomUUID();
    }
}
