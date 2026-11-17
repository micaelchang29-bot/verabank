# risk-service

Servicio Quarkus 3.40 (Java 21, modo JVM) con **una imagen y dos roles** (ADR-08): `scoring` (este documento, tarea 4.C) y
`stream` (consumidor de Kafka y reglas AML, tarea 4.E; hoy solo un marcador). El rol se elige al arrancar con
`VERABANK_ROLE=scoring|stream` (D3): los beans del rol se activan por configuración de ejecución (`@LookupIfProperty`), no
al compilar. Los paquetes `scoring` y `stream` no se importan entre sí (prueba ArchUnit).

## Rol scoring

`POST /internal/v1/scores` (contracts/openapi.yaml; solo red interna, sin API key):

- `200 {score, decision, model_version}`: `score` con 5 decimales. Idempotente por `transfer_id`: un reintento devuelve lo
  guardado sin recalcular.
- `400 validation-error` y `503 model-not-loaded` en `application/problem+json` con `correlation_id`
  (`X-Correlation-Id`, UUID).
- Features: dos consultas concurrentes y index-only sobre `risk.scoring_log` (`ix_slog_dest`, `ix_slog_source`),
  ventanas `[t - w, t)`; las filas `warmup` cuentan como historia (D1). Definiciones: `ml/README.md`.
- Inferencia: una sesión de ONNX Runtime (CPU, un hilo intra-op) en el pool de workers.
- Decisión: `round(score, 5)` con la regla exacta de `ml/README.md` («Contrato con el servicio de scoring», (a)):
  `new BigDecimal(p).setScale(5, HALF_EVEN)`; `BLOCK` si `>= t_block`, `REVIEW` si `>= t_review`, si no `APPROVE`.
- Sombra (D8, `verabank.shadow.enabled`): después de responder puntúa el challenger y escribe `shadow_score` solo si
  está vacío. Un fallo se cuenta en `scoring.shadow.failures` y no cambia la respuesta.

**Arranque.** Carga `model.onnx` y comprueba su SHA-256 contra `thresholds.json` y `manifest.json`, el de `thresholds.json`
contra `manifest.json`, la versión del champion y que `feature_order` sea exactamente el conjunto de features que el código
sabe calcular. Si algo no coincide el servicio no queda listo (`/q/health/ready` 503, `503 model-not-loaded`) y el log dice
qué falló, sin volcar contenido. Si el challenger no es válido, solo se desactiva la sombra.

**Salud y métricas.** `/q/health/ready` (modelo + PostgreSQL), `/q/health/live`, `/q/metrics`:
`scoring.stage.duration{stage=features|inference|log|total}` (histograma con el presupuesto de §1.2 como cubetas:
40, 10 y 10 ms y 150 ms en total), `scoring.decisions{decision}`, `scoring.errors{type}`, `scoring.shadow.failures`.
Hay una muestra de `log` por cada sentencia contra la bitácora: la lectura previa de idempotencia y la inserción.

## Configuración

| Variable | Por defecto | Qué hace |
|---|---|---|
| `VERABANK_ROLE` | `scoring` | `scoring` o `stream` |
| `VERABANK_SHADOW_ENABLED` | `false` | puntúa también el challenger |
| `VERABANK_QUERY_TIMEOUT` | `100ms` | tiempo máximo por consulta |
| `VERABANK_SCORING_POOL_SIZE` | `10` | conexiones del pool reactivo dedicado (`scoring`) |
| `POSTGRES_HOST` `POSTGRES_PORT` `POSTGRES_DB` | `localhost` `5432` `verabank` | base de datos |
| `VERABANK_DB_USER` `RISK_SVC_PASSWORD` | `risk_svc` y vacío | rol de ejecución (db/README.md) |
| `SERVER_PORT` | `8082` | puerto HTTP |

Los artefactos se leen de `ml/artifacts` al compilar (`pom.xml` lo declara como directorio de recursos; sin copias en el
repositorio) y viajan dentro del jar. `verabank.artifacts.dir` los lee de un directorio en su lugar.

## Compilar, probar y construir la imagen

```bash
make test-risk    # ./mvnw verify: unitarias e integración con Testcontainers PostgreSQL (necesita Docker)
make build-risk   # docker build -f risk-service/Dockerfile .  (contexto: la raíz, por ml/artifacts)
```

Las pruebas levantan PostgreSQL con los roles, migraciones y permisos reales de `db/`, y el servicio se conecta como
`risk_svc`. Cubren: ejemplos a mano de `FeatureComputer`; T-11 (límites de `t_review` y `t_block`, empates de redondeo);
T-13 con los 1.000 casos de `golden.jsonl` y de `golden_challenger.jsonl` y con los escenarios de
`contracts/fixtures/feature_scenarios.json` a través del servicio; `EXPLAIN` index-only; idempotencia secuencial y con 20
hilos; artefactos alterados; sombra; y que el rol `stream` no activa el scoring ni hay clientes de Kafka.
