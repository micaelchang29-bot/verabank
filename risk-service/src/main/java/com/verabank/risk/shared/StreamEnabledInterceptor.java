package com.verabank.risk.shared;

import io.smallrye.config.ConfigSourceInterceptor;
import io.smallrye.config.ConfigSourceInterceptorContext;
import io.smallrye.config.ConfigValue;

/**
 * Deriva del rol (D3) {@code verabank.stream.enabled} y el {@code enabled} de los canales de entrada de Kafka: el rol
 * scoring no abre ningún consumidor, sin una segunda variable que pueda quedar desalineada con {@code VERABANK_ROLE}.
 * Un interceptor y no una expresión {@code ${...}} en application.properties: Quarkus fija en la compilación las
 * propiedades de los canales y el rol solo se conoce al arrancar.
 */
public class StreamEnabledInterceptor implements ConfigSourceInterceptor {

    static final String NAME = "verabank.stream.enabled";
    static final java.util.Set<String> CHANNELS = java.util.Set.of(
            "mp.messaging.incoming.transfer-events.enabled", "mp.messaging.incoming.fraud-labels.enabled");

    @Override
    public ConfigValue getValue(ConfigSourceInterceptorContext context, String name) {
        if (!NAME.equals(name) && !CHANNELS.contains(name)) {
            return context.proceed(name);
        }
        ConfigValue role = context.proceed("verabank.role");
        boolean stream = role != null && "stream".equals(role.getValue());
        return ConfigValue.builder().withName(name).withValue(Boolean.toString(stream)).build();
    }
}
