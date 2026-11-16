package com.verabank.transfer.web;

import jakarta.servlet.http.HttpServletRequest;
import java.util.stream.Collectors;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataAccessException;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.HttpStatusCode;
import org.springframework.http.MediaType;
import org.springframework.http.ProblemDetail;
import org.springframework.http.ResponseEntity;
import org.springframework.lang.Nullable;
import org.springframework.transaction.TransactionException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.context.request.ServletWebRequest;
import org.springframework.web.context.request.WebRequest;
import org.springframework.web.method.annotation.MethodArgumentTypeMismatchException;
import org.springframework.web.servlet.mvc.method.annotation.ResponseEntityExceptionHandler;

/** Todos los errores salen en application/problem+json con type del contrato y correlation_id. */
@RestControllerAdvice
public class ApiExceptionHandler extends ResponseEntityExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(ApiExceptionHandler.class);

    @ExceptionHandler(ApiException.class)
    ResponseEntity<ProblemDetail> handleApi(ApiException ex, HttpServletRequest request) {
        ProblemDetail problem = Problems.of(ex.status(), ex.type(), ex.title(), ex.getMessage(),
                RequestContext.correlationId(request));
        HttpHeaders headers = new HttpHeaders();
        ex.headers().forEach(headers::add);
        return respond(ex.status(), headers, problem);
    }

    @ExceptionHandler(MethodArgumentTypeMismatchException.class)
    ResponseEntity<ProblemDetail> handleTypeMismatch(MethodArgumentTypeMismatchException ex,
            HttpServletRequest request) {
        return validation("El parámetro '" + ex.getName() + "' no tiene un formato válido.", request);
    }

    /** Cualquier fallo de acceso a datos o de transacción es 503 reintentable (§3.4). */
    @ExceptionHandler({DataAccessException.class, TransactionException.class})
    ResponseEntity<ProblemDetail> handleDatabaseDown(Exception ex, HttpServletRequest request) {
        log.error("base de datos no disponible o con error: {}", ex.getMessage());
        HttpHeaders headers = new HttpHeaders();
        headers.add(HttpHeaders.RETRY_AFTER, "1");
        return respond(HttpStatus.SERVICE_UNAVAILABLE, headers, Problems.of(HttpStatus.SERVICE_UNAVAILABLE,
                "service-unavailable", "Servicio no disponible",
                "La base de datos no está disponible; reintenta con la misma Idempotency-Key.",
                RequestContext.correlationId(request)));
    }

    @ExceptionHandler(Exception.class)
    ResponseEntity<ProblemDetail> handleUnexpected(Exception ex, HttpServletRequest request) {
        log.error("error no controlado", ex);
        return respond(HttpStatus.INTERNAL_SERVER_ERROR, new HttpHeaders(), Problems.of(
                HttpStatus.INTERNAL_SERVER_ERROR, "internal-error", "Error interno",
                "Error inesperado; consulta los logs con el correlation_id.", RequestContext.correlationId(request)));
    }

    @Override
    protected ResponseEntity<Object> handleMethodArgumentNotValid(MethodArgumentNotValidException ex,
            HttpHeaders headers, HttpStatusCode status, WebRequest request) {
        String detail = ex.getBindingResult().getFieldErrors().stream()
                .map(e -> snake(e.getField()) + ": " + e.getDefaultMessage())
                .sorted()
                .collect(Collectors.joining("; "));
        if (detail.isEmpty()) {
            detail = "Cuerpo inválido.";
        }
        return handleExceptionInternal(ex, problem(HttpStatus.BAD_REQUEST, detail, request), headers,
                HttpStatus.BAD_REQUEST, request);
    }

    /** Errores propios de Spring MVC (JSON ilegible, cabecera faltante, ruta inexistente…). */
    @Override
    protected ResponseEntity<Object> handleExceptionInternal(Exception ex, @Nullable Object body, HttpHeaders headers,
            HttpStatusCode status, WebRequest request) {
        ProblemDetail problem = body instanceof ProblemDetail pd && pd.getProperties() != null
                && pd.getProperties().containsKey("correlation_id")
                ? pd
                : problem(status, detailFor(ex, status), request);
        HttpHeaders out = new HttpHeaders();
        out.putAll(headers);
        out.setContentType(MediaType.APPLICATION_PROBLEM_JSON);
        return new ResponseEntity<>(problem, out, status);
    }

    private ProblemDetail problem(HttpStatusCode status, String detail, WebRequest request) {
        HttpServletRequest servlet = request instanceof ServletWebRequest swr ? swr.getRequest() : null;
        String type = switch (status.value()) {
            case 400 -> "validation-error";
            case 404 -> "not-found";
            case 405 -> "method-not-allowed";
            case 406 -> "not-acceptable";
            case 415 -> "unsupported-media-type";
            default -> "error";
        };
        String title = status.value() == 400 ? "Solicitud inválida" : HttpStatus.valueOf(status.value()).getReasonPhrase();
        return Problems.of(status, type, title, detail, servlet != null ? RequestContext.correlationId(servlet) : null);
    }

    private static String detailFor(Exception ex, HttpStatusCode status) {
        if (status.value() == 400) {
            return switch (ex.getClass().getSimpleName()) {
                case "HttpMessageNotReadableException" -> "El cuerpo no es JSON válido o algún campo tiene un tipo o valor no admitido.";
                case "MissingRequestHeaderException" -> ex.getMessage();
                default -> "Solicitud inválida.";
            };
        }
        if (status.value() == 404) {
            return "Recurso inexistente.";
        }
        return HttpStatus.valueOf(status.value()).getReasonPhrase();
    }

    private static ResponseEntity<ProblemDetail> respond(HttpStatusCode status, HttpHeaders headers,
            ProblemDetail problem) {
        headers.setContentType(MediaType.APPLICATION_PROBLEM_JSON);
        return new ResponseEntity<>(problem, headers, status);
    }

    private static ResponseEntity<ProblemDetail> validation(String detail, HttpServletRequest request) {
        return respond(HttpStatus.BAD_REQUEST, new HttpHeaders(), Problems.of(HttpStatus.BAD_REQUEST,
                "validation-error", "Solicitud inválida", detail, RequestContext.correlationId(request)));
    }

    private static String snake(String camel) {
        return camel.replaceAll("([a-z0-9])([A-Z])", "$1_$2").toLowerCase();
    }
}
