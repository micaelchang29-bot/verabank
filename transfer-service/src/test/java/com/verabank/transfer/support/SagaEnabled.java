package com.verabank.transfer.support;

import java.lang.annotation.Documented;
import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.annotation.DirtiesContext;

/**
 * Activa relay del outbox, sondeo de la saga y consumidor de rail.replies.v1 en la clase de prueba,
 * con tiempos cortos, y cierra su contexto al terminar (solo hay un contexto activo a la vez).
 * Cada clase ajusta los tiempos que necesita con {@code @TestPropertySource}.
 */
@Documented
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.TYPE)
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT, properties = {
        "verabank.outbox.relay-enabled=true",
        "verabank.saga.poller-enabled=true",
        "verabank.rail.replies-enabled=true",
        "verabank.rail.retry-interval=200ms",
        "verabank.saga.poller-interval=200ms",
        "verabank.saga.resume-after=2s",
        "verabank.saga.rail-reply-timeout=1s",
        "verabank.saga.poll-backoff-base=500ms",
        "verabank.saga.poll-backoff-max=2s",
        "verabank.saga.backoff-base=300ms",
        "verabank.saga.backoff-max=2s",
        "verabank.saga.stuck-refresh=1s"})
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
public @interface SagaEnabled {
}
