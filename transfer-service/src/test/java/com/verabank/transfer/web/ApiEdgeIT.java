package com.verabank.transfer.web;

import static org.assertj.core.api.Assertions.assertThat;

import com.verabank.transfer.support.Http;
import com.verabank.transfer.support.IntegrationTest;
import java.util.Map;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.springframework.boot.test.system.CapturedOutput;
import org.springframework.boot.test.system.OutputCaptureExtension;

@ExtendWith(OutputCaptureExtension.class)
class ApiEdgeIT extends IntegrationTest {

    private Http http;

    @BeforeEach
    void setUp() {
        http = new Http(port);
    }

    @Test
    void sinApiKeyResponde401UnauthorizedEnProblemJson() {
        Http.Response r = http.get(null, "/api/v1/accounts/" + UUID.randomUUID());

        assertThat(r.status()).isEqualTo(401);
        assertThat(r.header("Content-Type")).startsWith("application/problem+json");
        assertThat(r.json().path("type").asText()).isEqualTo("unauthorized");
        assertThat(r.json().path("status").asInt()).isEqualTo(401);
        assertThat(r.json().path("title").asText()).isNotBlank();
        assertThat(r.json().path("detail").asText()).isNotBlank();
        assertThat(UUID.fromString(r.json().path("correlation_id").asText())).isNotNull();
    }

    @Test
    void conApiKeyInvalidaResponde401() {
        Http.Response r = http.get("no-es-una-clave-valida-123", "/api/v1/accounts/" + UUID.randomUUID());

        assertThat(r.status()).isEqualTo(401);
        assertThat(r.json().path("type").asText()).isEqualTo("unauthorized");
    }

    @Test
    void elCorrelationIdDeLaCabeceraVuelveEnLaRespuestaYEnElError() {
        UUID cid = UUID.randomUUID();
        Http.Response r = http.send("GET", "/api/v1/accounts/" + UUID.randomUUID(),
                Map.of("X-Correlation-Id", cid.toString()), null);

        assertThat(r.status()).isEqualTo(401);
        assertThat(r.header("X-Correlation-Id")).isEqualTo(cid.toString());
        assertThat(r.json().path("correlation_id").asText()).isEqualTo(cid.toString());
    }

    @Test
    void sinCorrelationIdSeGeneraUno() {
        Http.Response r = http.get(null, "/api/v1/accounts/" + UUID.randomUUID());

        String generated = r.header("X-Correlation-Id");
        assertThat(UUID.fromString(generated)).isNotNull();
        assertThat(r.json().path("correlation_id").asText()).isEqualTo(generated);
    }

    @Test
    void unCorrelationIdQueNoEsUuidSeReemplaza() {
        Http.Response r = http.send("GET", "/api/v1/accounts/" + UUID.randomUUID(),
                Map.of("X-Correlation-Id", "abc"), null);

        assertThat(UUID.fromString(r.header("X-Correlation-Id"))).isNotNull();
    }

    @Test
    void lasClavesNuncaAparecenEnLosLogs(CapturedOutput output) {
        http.get(KEY_WEB, "/api/v1/ruta-inexistente");
        http.get(KEY_MOBILE + "x", "/api/v1/accounts/" + UUID.randomUUID());
        http.postTransfer(KEY_MOBILE, UUID.randomUUID(), "{}");

        assertThat(output.getAll())
                .doesNotContain(KEY_WEB)
                .doesNotContain(KEY_MOBILE);
    }

    @Test
    void rutaInexistenteConClaveValidaEs404EnProblemJson() {
        Http.Response r = http.get(KEY_WEB, "/api/v1/ruta-inexistente");

        assertThat(r.status()).isEqualTo(404);
        assertThat(r.header("Content-Type")).startsWith("application/problem+json");
        assertThat(r.json().path("correlation_id").asText()).isEqualTo(r.header("X-Correlation-Id"));
    }

    @Test
    void laSaludNoExigeClave() {
        assertThat(http.get(null, "/actuator/health").status()).isEqualTo(200);
    }
}
