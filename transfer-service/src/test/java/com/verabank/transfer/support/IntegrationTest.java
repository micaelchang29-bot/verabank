package com.verabank.transfer.support;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

/** Base de las pruebas de integración: servicio real sobre PostgreSQL y Kafka en contenedores. */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
public abstract class IntegrationTest {

    public static final String CLIENT_WEB = "web";
    public static final String KEY_WEB = "test-key-web-6f1d2c9a7b3e";
    public static final String CLIENT_MOBILE = "mobile";
    public static final String KEY_MOBILE = "test-key-mobile-0c4b8e2f9d1a";

    static {
        Infra.init();
    }

    @DynamicPropertySource
    static void infraProperties(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", Infra.POSTGRES::getJdbcUrl);
        r.add("spring.datasource.username", () -> Infra.TRANSFER_USER);
        r.add("spring.datasource.password", () -> Infra.TRANSFER_PASSWORD);
        r.add("spring.kafka.bootstrap-servers", Infra.KAFKA::getBootstrapServers);
        r.add("verabank.migrations-dir",
                () -> Infra.repoPath("verabank.migrations.dir", "../db/migrations").toString());
        r.add("verabank.api-keys", () -> CLIENT_WEB + ":" + KEY_WEB + "," + CLIENT_MOBILE + ":" + KEY_MOBILE);
        r.add("verabank.saga.resume-after", () -> "2s");
    }

    @LocalServerPort
    protected int port;

    @Autowired
    protected JdbcClient jdbc;
}
