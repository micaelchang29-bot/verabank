# risk-service

Servicio Quarkus 3.40 (Java 21, modo JVM) con **una imagen y dos roles** (ADR-08): `scoring` (tarea 4.C) y `stream`
(consumidores de Kafka, reglas AML, alertas y etiquetas, tarea 4.E). El rol se elige al arrancar con
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

**Calentamiento de arranque.** Antes de declarar `ready`, el rol scoring (1) crea sus métricas, (2) retiene a la vez
todas las conexiones del pool y en cada una ejecuta una consulta de cada tipo de la ruta (lectura de la bitácora,
destino, origen y el INSERT en una transacción revertida) y (3) hace `VERABANK_WARMUP_ROUNDS` pasadas sintéticas
completas. No escribe en `risk.scoring_log`. La readiness solo sube cuando terminó (`WarmUpPendingIT`); la primera
solicitud real tras `ready` responde por debajo de los 120 ms de transfer-service (`FirstCallLatencyIT`).
Si la base no estaba lista y el calentamiento falla o se agota su tiempo, devuelve al pool todas las conexiones que
llegó a abrir, también las que llegan tarde, y la readiness lo reintenta hasta lograrlo. Antes se quedaban fuera del
pool y el pod no llegaba nunca a `ready` (D16 en kind; `WarmUpAfterFailedConnectionsIT`, `WarmUpAfterLateConnectionsIT`).

## Rol stream

`VERABANK_ROLE=stream`: no carga ONNX Runtime ni el modelo (D3) y su pool de PostgreSQL es propio (`stream`, rol `risk_svc`).
Los canales de Kafka solo se abren con este rol (`StreamEnabledInterceptor` deriva su `enabled` del rol).

**Consumidores** (Reactive Messaging, commit manual: el offset se confirma al reconocer el mensaje, después de confirmar la
transacción local y de publicar las alertas):

| Tópico | Grupo | Efecto |
|---|---|---|
| `transfer.events.v1` | `risk-stream` | `TransferCompleted`: inbox (`risk.processed_messages`, `risk-stream:transfer-events`) + `INSERT … ON CONFLICT DO NOTHING` en `risk.transfer_facts` + cadena de reglas + upsert de alertas, en UNA transacción. Cualquier otro `event_type` se reconoce sin efecto. |
| `fraud.labels.v1` | `risk-stream-labels` | `FraudLabelConfirmed`: inbox + upsert en `risk.fraud_labels`; una etiqueta con otro valor o fuente actualiza `is_fraud` y `labeled_at`. |

Errores: un mensaje que no valida contra `envelope.v1.json` y el esquema de su payload va directo a `<tópico>.dlt` (partición 0,
sin reintentos); cualquier otro fallo se reintenta 3 veces (`VERABANK_STREAM_RETRIES`, espera `VERABANK_STREAM_RETRY_BACKOFF`) y
después va al `.dlt` con `x-original-topic/-partition/-offset` y `x-exception-class/-message`. Los esquemas viajan en el jar,
copiados de `contracts/events` al compilar.

**Cadena de reglas** (`AmlRule`, cadena de responsabilidad; `docs/diseno.md` §5a). Los parámetros salen de la versión activa de
`risk.aml_rules`, en caché y releída cada 60 s (`VERABANK_RULES_REFRESH`); cada alerta guarda `rule_version` y `rule_params`.
Todas las ventanas usan el tiempo de negocio del evento (`occurred_at`), nunca `now()`.

| Orden | Regla | Comportamiento |
|---|---|---|
| 10 | `ExemptionRule` | Origen en `risk.aml_exempt_accounts`: corta la cadena (el hecho sí se proyecta). |
| 20 | `ThresholdRule` | Monto ≥ umbral → `MEDIUM`; `dedupe_key = THRESHOLD\|origen\|transfer_id`. |
| 30 | `StructuringRule` | Mismo origen en `[t − 24 h, t]` con `min_ratio ≤ monto/umbral < max_ratio` (máximo exclusivo): ≥ `min_count` y suma ≥ `min_total` → `HIGH`; `dedupe_key = STRUCTURING\|origen\|occurred_at de la primera`. El upsert reemplaza `transfer_ids`, `total_amount` y `window_end`; conserva `created_at`, `status` y `trigger_emitted_at`. |
| 40 | `CountryRiskRule` | País con nivel vigente en `occurred_at` en `risk.country_risk` (`[valid_from, valid_to)`): `HIGH` con monto ≥ 1.000 → `HIGH`; `WATCH` con monto ≥ 5.000 → `MEDIUM`. |

Una regla nueva (la futura `FAN_IN`) es un bean más que extiende `AccumulatingRule`: `AmlChain` las ordena por `order()` y no se
toca ninguna existente (`AmlRulesIT.aNewRuleJoinsTheChainWithoutTouchingTheExistingOnes` lo demuestra con una regla ficticia).
Una regla sin versión activa (hoy `FAN_IN`) se omite.

**Publicación.** `AmlAlertRaised` en `aml.alerts.v1`, clave `subject_account_id`, productor idempotente con `acks=all`, solo
después de confirmar la transacción y solo si la alerta es nueva o cambió su conjunto de transferencias (D6). Si Kafka no
confirma tras los reintentos, la alerta ya está guardada y el mensaje de origen va al `.dlt` (reprocesarlo no republica: el
inbox ya lo registró).

**API** (`contracts/openapi.yaml`, API key por canal `VERABANK_API_KEYS=canal:clave,…` como transfer-service; sin clave, `401`):
`GET /api/v1/aml/alerts` (filtros `status`, `severity`, `rule_code`; `limit` 1–200; cursor opaco; orden: severidad descendente
y, a igual severidad, la más antigua primero) y `PATCH /api/v1/aml/alerts/{id}` con `{status, comment}`. Transiciones:
`OPEN → IN_REVIEW`; `IN_REVIEW → ESCALATED | CLOSED_FALSE_POSITIVE | CLOSED_CONFIRMED`; `ESCALATED → CLOSED_*`; otra,
`409 alert-transition-invalid`. Cada cambio se audita con `audit.append` (`AML_ALERT_STATUS_CHANGED`, actor = canal) y al cerrar
se fijan `closed_at` y `closed_by` (el canal).

**Métricas** (`/q/metrics`): `stream_consumer_delay_seconds{topic}` (ahora − `emitted_at`), `kafka_consumer_fetch_manager_records_lag*`
(binder de Kafka), `aml_alerts_raised_total{rule_code}`, `aml_alerts_updated_total{rule_code}`,
`aml_alert_persist_latency_seconds` (`created_at − trigger_emitted_at`, cubetas hasta 5 s), `stream_dlt_total{topic,reason}`,
`stream_processing_attempts_total{consumer}`, `stream_inbox_duplicates_total{consumer}`, `aml_alerts_publish_failures_total`.
**Readiness** (`/q/health/ready`, comprobación `stream`): PostgreSQL y Kafka.

## Configuración

| Variable | Por defecto | Qué hace |
|---|---|---|
| `VERABANK_ROLE` | `scoring` | `scoring` o `stream` |
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | broker (rol stream) |
| `VERABANK_API_KEYS` | vacío | `canal:clave,…`; obligatoria con el rol stream (≥ 16 caracteres por clave) |
| `VERABANK_STREAM_RETRIES` `VERABANK_STREAM_RETRY_BACKOFF` | `3` `1s` | reintentos antes del `.dlt` |
| `VERABANK_RULES_REFRESH` | `60s` | refresco de la caché de `risk.aml_rules` |
| `VERABANK_STREAM_POOL_SIZE` | `10` | conexiones del pool `stream` |
| `VERABANK_SHADOW_ENABLED` | `false` | puntúa también el challenger |
| `VERABANK_QUERY_TIMEOUT` | `100ms` | tiempo máximo por consulta |
| `VERABANK_WARMUP_ROUNDS` | `200` | pasadas sintéticas completas (parser, features, inferencia, INSERT revertido, respuesta serializada) antes de la readiness del rol scoring |
| `VERABANK_WARMUP_TIMEOUT` | `30s` | tiempo máximo de todo el calentamiento; si se agota, la readiness sigue abajo y lo reintenta |
| `VERABANK_SCORING_POOL_SIZE` | `10` | conexiones del pool reactivo dedicado (`scoring`) |
| `POSTGRES_HOST` `POSTGRES_PORT` `POSTGRES_DB` | `localhost` `5432` `verabank` | base de datos |
| `VERABANK_DB_USER` `RISK_SVC_PASSWORD` | `risk_svc` y vacío | rol de ejecución (db/README.md) |
| `SERVER_PORT` | `8082` | puerto HTTP |

Los artefactos se leen de `ml/artifacts` al compilar (`pom.xml` lo declara como directorio de recursos; sin copias en el
repositorio) y viajan dentro del jar. `verabank.artifacts.dir` los lee de un directorio en su lugar.

## Compilar, probar y construir la imagen

```bash
make test-risk    # ./mvnw verify: unitarias e integración con Testcontainers PostgreSQL y Kafka (necesita Docker)
make build-risk   # docker build -f risk-service/Dockerfile .  (contexto: la raíz, por ml/artifacts)
```

Las pruebas levantan PostgreSQL con los roles, migraciones y permisos reales de `db/`, y el servicio se conecta como
`risk_svc`. Cubren: ejemplos a mano de `FeatureComputer`; T-11 (límites de `t_review` y `t_block`, empates de redondeo);
T-13 con los 1.000 casos de `golden.jsonl` y de `golden_challenger.jsonl` y con los escenarios de
`contracts/fixtures/feature_scenarios.json` a través del servicio; `EXPLAIN` index-only; idempotencia secuencial y con 20
hilos; artefactos alterados; sombra; y que el rol `stream` no carga el modelo ni el rol `scoring` abre consumidores. Con
Kafka real, el rol `stream`: T-15, bordes de las tres reglas, versiones activas, exención, reentrega, DLT y reintentos,
etiquetas, API de alertas con auditoría, contratos y latencia evento → alerta (200 eventos, `target/latency-report.txt`).

## Despliegue en Compose y prueba de carga

`make up` levanta todo en Compose (perfiles `core`, `risk` y `sim`): PostgreSQL, Kafka, `transfer-service` con
`VERABANK_SCORING_MODE=remote`, `risk-scoring` (`VERABANK_ROLE=scoring`, contenedor 8081, host `RISK_SCORING_PORT`=8083
porque el 8081 del host es de `transfer-service`), `risk-stream` (8082) y el rail simulado. Las dos imágenes de risk son la
misma (`verabank/risk-service:dev`). Healthchecks con `/q/health/ready`; límites de memoria: scoring 768 MB (montículo de
256 MB), stream 640 MB. `transfer-service` no espera a `risk-scoring` (si no responde decide el fallback; ADR-04) porque sus
migraciones de core son las que `up` necesita antes de migrar risk. `make up-core` sigue arrancando `transfer-service` en el
host con scoring `stub` (lo usa la CI).

- `make perf-scoring [DURATION=30m] [PEAK=1]`: k6 (`perf/scoring.js`, imagen `grafana/k6:1.4.2`) contra `risk-scoring` dentro de
  la red de Compose; salidas en `data/perf/`.
- `make fallback-demo`: para `risk-scoring`, 500 filas del replay deben salir `FALLBACK`; al volver, mide cuánto tarda en
  haber una tanda entera con `MODEL`.
- Resultados medidos: `docs/resultados/fase-4.md`. Decisiones: `docs/adr/ADR-12.md`.
- El rol `risk_svc` tiene `statement_timeout = 250ms` en el servidor (respaldo del límite de 100 ms del cliente).
