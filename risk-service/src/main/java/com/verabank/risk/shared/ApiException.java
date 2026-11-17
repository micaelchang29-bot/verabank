package com.verabank.risk.shared;

/** Error de negocio con el {@code type} de contracts/openapi.yaml; se responde como application/problem+json. */
public class ApiException extends RuntimeException {

    private final int status;
    private final String type;
    private final String title;

    public ApiException(int status, String type, String title, String detail) {
        super(detail);
        this.status = status;
        this.type = type;
        this.title = title;
    }

    public ApiException(int status, String type, String title, String detail, Throwable cause) {
        super(detail, cause);
        this.status = status;
        this.type = type;
        this.title = title;
    }

    public int status() {
        return status;
    }

    public String type() {
        return type;
    }

    public String title() {
        return title;
    }

    public static ApiException validation(String detail) {
        return new ApiException(400, "validation-error", "Solicitud inválida", detail);
    }

    public static ApiException modelNotLoaded(String detail) {
        return new ApiException(503, "model-not-loaded", "Modelo no cargado", detail);
    }

    public static ApiException notFound(String detail) {
        return new ApiException(404, "not-found", "No encontrado", detail);
    }
}
