package com.verabank.transfer.web;

import java.util.Map;
import org.springframework.http.HttpStatus;

/** Error de negocio con su {@code type} del contrato; lo traduce {@link ApiExceptionHandler}. */
public class ApiException extends RuntimeException {

    private final HttpStatus status;
    private final String type;
    private final String title;
    private final Map<String, String> headers;

    public ApiException(HttpStatus status, String type, String title, String detail) {
        this(status, type, title, detail, Map.of());
    }

    public ApiException(HttpStatus status, String type, String title, String detail, Map<String, String> headers) {
        super(detail);
        this.status = status;
        this.type = type;
        this.title = title;
        this.headers = Map.copyOf(headers);
    }

    public static ApiException validation(String detail) {
        return new ApiException(HttpStatus.BAD_REQUEST, "validation-error", "Solicitud inválida", detail);
    }

    public HttpStatus status() {
        return status;
    }

    public String type() {
        return type;
    }

    public String title() {
        return title;
    }

    public Map<String, String> headers() {
        return headers;
    }
}
