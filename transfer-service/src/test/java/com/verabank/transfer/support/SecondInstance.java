package com.verabank.transfer.support;

import com.verabank.transfer.TransferServiceApplication;
import java.util.ArrayList;
import java.util.List;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.context.ConfigurableApplicationContext;

/**
 * Otra instancia del servicio contra la misma base y el mismo Kafka, para probar la competencia
 * por el sondeo. Arranca con el relay y el consumidor apagados: solo el sondeo (y la API, en un puerto libre).
 */
public final class SecondInstance {

    private SecondInstance() {
    }

    public static ConfigurableApplicationContext start(String... extra) {
        List<String> args = new ArrayList<>(List.of(
                "--server.port=0",
                "--spring.datasource.url=" + Infra.POSTGRES.getJdbcUrl(),
                "--spring.datasource.username=" + Infra.TRANSFER_USER,
                "--spring.datasource.password=" + Infra.TRANSFER_PASSWORD,
                "--spring.kafka.bootstrap-servers=" + Infra.KAFKA.getBootstrapServers(),
                "--verabank.migrations-dir=" + Infra.repoPath("verabank.migrations.dir", "../db/migrations"),
                "--verabank.contracts-dir=" + Infra.repoPath("verabank.contracts.dir", "../contracts"),
                "--verabank.api-keys=" + IntegrationTest.CLIENT_WEB + ":" + IntegrationTest.KEY_WEB,
                "--verabank.outbox.relay-enabled=false",
                "--verabank.rail.replies-enabled=false",
                "--verabank.saga.poller-enabled=true"));
        args.addAll(List.of(extra));
        return new SpringApplicationBuilder(TransferServiceApplication.class).run(args.toArray(String[]::new));
    }
}
