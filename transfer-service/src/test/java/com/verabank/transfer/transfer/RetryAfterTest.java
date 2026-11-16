package com.verabank.transfer.transfer;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;

/** Retry-After = clamp(wake_at - ahora, 1, 5) segundos. */
class RetryAfterTest {

    @ParameterizedTest(name = "faltan {0} s -> Retry-After {1}")
    @CsvSource({
        "-30, 1",    // wake_at ya vencido
        "0, 1",
        "1, 1",      // extremo inferior
        "2, 2",
        "5, 5",      // extremo superior
        "6, 5",
        "30, 5",     // resume-after por defecto
        "3600, 5",   // wake_at lejano
    })
    void acotaEntre1Y5Segundos(long remaining, long expected) {
        assertThat(TransferService.clampRetryAfter(remaining)).isEqualTo(expected);
    }
}
