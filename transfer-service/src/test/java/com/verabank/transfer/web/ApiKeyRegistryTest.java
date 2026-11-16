package com.verabank.transfer.web;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.verabank.transfer.config.VerabankProperties;
import org.junit.jupiter.api.Test;

class ApiKeyRegistryTest {

    private static ApiKeyRegistry registry(String spec) {
        return new ApiKeyRegistry(new VerabankProperties(null, null, spec, null, null, null));
    }

    @Test
    void resuelveElCanalDeCadaClave() {
        ApiKeyRegistry r = registry("web:aaaaaaaaaaaaaaaa1, mobile:bbbbbbbbbbbbbbbb2");

        assertThat(r.resolve("aaaaaaaaaaaaaaaa1")).contains("web");
        assertThat(r.resolve("bbbbbbbbbbbbbbbb2")).contains("mobile");
        assertThat(r.resolve("cccccccccccccccc3")).isEmpty();
        assertThat(r.resolve(null)).isEmpty();
        assertThat(r.resolve("")).isEmpty();
    }

    @Test
    void sinClavesNoArranca() {
        assertThatThrownBy(() -> registry("")).isInstanceOf(IllegalStateException.class);
        assertThatThrownBy(() -> registry(null)).isInstanceOf(IllegalStateException.class);
    }

    @Test
    void losErroresDeConfiguracionNoIncluyenLaClave() {
        String secret = "clave-secreta-de-prueba";
        assertThatThrownBy(() -> registry("Web:" + secret)).hasMessageNotContaining(secret);
        assertThatThrownBy(() -> registry("web:" + secret + ",mobile:" + secret)).hasMessageNotContaining(secret);
        assertThatThrownBy(() -> registry("web:" + secret + ",web:" + secret + "x")).hasMessageNotContaining(secret);
        assertThatThrownBy(() -> registry("web:corta")).hasMessageNotContaining("corta");
        assertThatThrownBy(() -> registry(secret)).hasMessageNotContaining(secret);
    }
}
