package com.verabank.transfer.support;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;

/** Cliente HTTP mínimo para las pruebas (sin pasar por MockMvc: incluye los filtros reales). */
public final class Http {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final HttpClient CLIENT = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5))
            .build();

    private final int port;

    public Http(int port) {
        this.port = port;
    }

    public record Response(int status, Map<String, String> headers, String body) {

        public JsonNode json() {
            try {
                return MAPPER.readTree(body);
            } catch (IOException e) {
                throw new IllegalStateException("cuerpo no JSON: " + body, e);
            }
        }

        public String header(String name) {
            return headers.get(name.toLowerCase());
        }
    }

    public Response postTransfer(String apiKey, UUID idempotencyKey, String body) {
        Map<String, String> headers = new LinkedHashMap<>();
        if (apiKey != null) {
            headers.put("X-API-Key", apiKey);
        }
        if (idempotencyKey != null) {
            headers.put("Idempotency-Key", idempotencyKey.toString());
        }
        return send("POST", "/api/v1/transfers", headers, body);
    }

    public Response review(String apiKey, UUID transferId, String body) {
        return send("POST", "/api/v1/transfers/" + transferId + "/review",
                apiKey == null ? Map.of() : Map.of("X-API-Key", apiKey), body);
    }

    public Response post(String apiKey, String path) {
        return send("POST", path, apiKey == null ? Map.of() : Map.of("X-API-Key", apiKey), null);
    }

    public Response get(String apiKey, String path) {
        return send("GET", path, apiKey == null ? Map.of() : Map.of("X-API-Key", apiKey), null);
    }

    public Response send(String method, String path, Map<String, String> headers, String body) {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
                .timeout(Duration.ofSeconds(30));
        headers.forEach(b::header);
        if (body != null) {
            b.header("Content-Type", "application/json");
            b.method(method, HttpRequest.BodyPublishers.ofString(body));
        } else {
            b.method(method, HttpRequest.BodyPublishers.noBody());
        }
        try {
            HttpResponse<String> r = CLIENT.send(b.build(), HttpResponse.BodyHandlers.ofString());
            Map<String, String> h = new LinkedHashMap<>();
            r.headers().map().forEach((k, v) -> h.put(k.toLowerCase(), String.join(",", v)));
            return new Response(r.statusCode(), h, r.body());
        } catch (IOException e) {
            throw new IllegalStateException(e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException(e);
        }
    }
}
