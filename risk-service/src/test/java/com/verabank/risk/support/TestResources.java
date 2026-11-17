package com.verabank.risk.support;

import io.quarkus.test.common.QuarkusTestResource;

/** Registra el PostgreSQL de pruebas para todas las pruebas de Quarkus. */
@QuarkusTestResource(PostgresResource.class)
public class TestResources {
}
