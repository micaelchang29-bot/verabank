# Resultados de la fase 4

Mediciones propias de la corrida de aceptación del 2026-11-17, en la laptop de referencia (WSL2, 12 núcleos visibles, 15 GB). Todo en Compose con `make up`. La columna «objetivo» es la de `docs/diseno.md` §1.2; no es un resultado.

Hubo dos pasadas. La **primera** se hizo antes del calentamiento de arranque de `risk-scoring` y `transfer-service`. La **segunda**, sobre una base y un Kafka nuevos (volúmenes borrados con confirmación), con el calentamiento. Las dos se conservan. Las cifras salen de `data/replay/summary.json`, `data/perf/` y las salidas de `make`, sin estimaciones.

**Primera pasada: no reproducible: cifras reportadas en su momento, base descartada.** Sus cifras (la columna «primera pasada» de las tablas a, c y d, las filas «Primera» de b y los dos primeros renglones de memoria) son las reportadas en su momento; la base y Kafka se descartaron y no hay archivos de respaldo para recomprobarlas. Solo la segunda pasada se auditó contra `data/replay/summary.json`, `data/perf/` y SQL.

## a. Replay de PaySim (`make replay N=20000 TPS=50`)

Parámetros: partición de prueba, pasos 575 a 590, `AMOUNT_FACTOR=1`, calentamiento de 26.503 filas (`COPY`), 20.000 cuentas sembradas.

| Dato | Primera pasada (arranque en frío; no reproducible) | Segunda pasada (con calentamiento) |
|---|---|---|
| Enviadas / duración / TPS | 20.000 / 405,8 s / 49,28 | 20.000 / 404,0 s / 49,50 |
| Código HTTP | `201`: 19.873; `422`: 127 | `201`: 19.871; `422`: 129 |
| `fraud_decision` | `APPROVE` 19.167, `REVIEW` 706, `BLOCK` 127 | `APPROVE` 19.591, `REVIEW` 280, `BLOCK` 129 |
| `decision_source` | `MODEL` 19.495, **`FALLBACK` 505** | `MODEL` 19.997, **`FALLBACK` 3** |
| Estado final | `COMPLETED` 19.167, `IN_REVIEW` 706, `REJECTED` 127 | `COMPLETED` 19.591, `IN_REVIEW` 280, `REJECTED` 129 |
| `POST /transfers` (cliente) | p50 23,7 / p95 57,0 / p99 130,4 ms | p50 22,6 / p95 54,9 / p99 131,0 ms. Objetivo: p95 < 400 ms |
| `scoring_latency_ms` | p50 6 / p95 13 / p99 21 | p50 6 / p95 12 / p99 20 |
| Etiquetas publicadas | 20.000 | 20.000 |
| `make reconcile` | 0 y 0 | 0 y 0 |

**Los 505 `FALLBACK` de la primera pasada** estaban en los primeros 11 s (más 3 sueltos): las primeras llamadas de `transfer-service` a `risk-scoring` superaban el timeout de 120 ms y el circuito abría 10 s. El fallback aprueba hasta 500 PEN y manda a revisión lo que pasa de ahí, por eso los `REVIEW` bajan de 706 a 280 al desaparecer casi todos. **Los 3 de la segunda**: dos en los primeros 8 s del replay (123 y 146 ms de latencia de scoring) y uno aislado a los 4 min (135 ms).

### Matriz decisión × `is_fraud` (segunda pasada, etiquetas de `risk.fraud_labels`)

| Decisión | Legítimas | Fraude | Total |
|---|---:|---:|---:|
| `APPROVE` | 19.540 | 51 | 19.591 |
| `REVIEW` | 238 | 42 | 280 |
| `BLOCK` | 53 | 76 | 129 |
| Total | 19.831 | 169 | 20.000 |

Comparación con `ml/reports/test_evaluation.json` (partición de test completa, 124.990 filas, modelo `fraud-xgb/4`; el servicio sirve `fraud-xgb/6`, que tiene el mismo `model_sha256` (reentrenamiento determinista, ver ADR-11), con los mismos umbrales `t_review = 0,83142` y `t_block = 0,9038`) y con las **mismas 20.000 filas puntuadas fuera de línea con historia completa** (el pipeline de `ml/`, con las features de `data/features/features_A.parquet`):

| Métrica | `test_evaluation.json` (test completo) | Mismas 20.000 filas, fuera de línea | Replay (servicio) |
|---|---:|---:|---:|
| Tasa de revisión | 1,00 % | 0,98 % (195) | 1,40 % (280) |
| Tasa de bloqueo | 0,455 % | 0,42 % (84) | 0,645 % (129) |
| FPR de bloqueo | 0,0154 % (19 legítimas) | 0,050 % (10 legítimas) | **0,267 %** (53 legítimas) |
| Recall de bloqueo | 29,5 % | 43,8 % | 45,0 % |
| Recall de revisión o bloqueo | 48,2 % | 60,4 % | 69,8 % |
| Prevalencia de fraude | 1,49 % | 0,845 % | 0,845 % |

Con el mismo código, la partición de test completa se reproduce exactamente (tasa de revisión 1,0009 %, FPR de bloqueo 0,0154 %, recall de bloqueo 29,54 %).

**Límite medido de D1.** El replay ve menos historia que el entrenamiento y las decisiones lo reflejan:

| Feature (promedio sobre las mismas 20.000 filas) | Historia completa (entrenamiento) | Servicio en el replay |
|---|---:|---:|
| `dst_seen_before` | 0,44 | 0,018 |
| `dst_age_hours` | 163 h | 0,29 h |
| `is_new_beneficiary` | 1,00 | 1,00 |

Casi todos los destinos parecen nuevos. El tramo (pasos 575 a 590) explica el recall más alto y parte del FPR (de 0,015 % a 0,050 % con historia completa); el desajuste de historia multiplica por 5 el FPR de bloqueo (de 0,050 % a 0,267 %) y por 1,4 la tasa de revisión. Las 53 legítimas bloqueadas son todas `TRANSFER`. Las decisiones del replay **no son comparables** con las de `test`. D1 se mantiene y el replay no cambia (ADR-12).

**Tope de revisión.** `docs/diseno.md` §5c fija `t_review` con una tasa de revisión ≤ 0,5 % (la capacidad supuesta de los analistas). La tasa de revisión medida de los umbrales entrenados en la fase 3 sobre `test` es 1,00 % (el doble; ADR-11 ya registra que los umbrales de `valid` no se trasladan limpiamente a `test`). En el replay es 1,40 %. No coincide con el tope; no se ha ajustado nada.

## b. Scoring con k6 (`POST /internal/v1/scores` directo a `risk-scoring`)

Comando: `make perf-scoring` (5 min) y `DURATION=30m PEAK=1 make perf-scoring`. Calentamiento de 60 s sin umbrales. Pool de 10 conexiones, `query-timeout` de 100 ms. Objetivos: p95 < 150 ms, p99 < 300 ms, errores < 1 %, 50 TPS durante 30 min y pico de 100 TPS durante 5 min.

| Corrida | Carga | Solicitudes | p95 | p99 | Errores |
|---|---|---:|---:|---:|---:|
| Primera, 5 min (no reproducible) | 50 TPS | 15.000 | 6,09 ms | 8,82 ms | 14 (0,09 %) |
| Primera, 30 min (no reproducible) | 50 TPS | 90.000 | 6,39 ms | 8,61 ms | 25 (0,03 %) |
| Primera, pico (no reproducible) | 100 TPS, 5 min | 30.000 | 6,89 ms | 10,45 ms | 0 |
| Segunda, 5 min | 50 TPS | 15.000 | 4,71 ms | 6,10 ms | 0 |
| Segunda, 30 min | 50 TPS | 90.000 | 4,54 ms | 5,43 ms | 16 (0,018 %) |
| Segunda, pico | 100 TPS, 5 min | 30.000 | 4,31 ms | 5,42 ms | 0 |

Todos los umbrales de k6 se cumplen en las seis corridas.

**Marcas de tiempo de los fallos (segunda, 30 min).** Los 16 fallos son respuestas `500 internal-error` («La consulta 'bitácora (escritura|lectura)' superó 100 ms») y caen **todos en una sola ráfaga de 0,3 s**, del segundo 739,3 al 739,6 de la prueba (2026-11-17T23:56:46Z, a los ~12 min), con 101 a 117 ms de latencia: 10 de escritura y 6 de lectura. No están al inicio ni son periódicos: es un único bloqueo breve de las consultas a PostgreSQL. No se investigó la causa. (En la primera pasada los fallos no se registraban con marca de tiempo.)

**Memoria** (`docker stats --no-stream`, una muestra cada 30 s durante la carga; suma de los seis contenedores). Objetivo: Compose ≤ 8 GB.

| Corrida | Muestras | Total mín. / máx. | Máximo por servicio |
|---|---:|---|---|
| Primera, 5 min (no reproducible) | 11 | 1.925 / 1.942 MiB | kafka 557, transfer-service 449, postgres 342, risk-scoring 284, risk-stream 282, rail-sim 32 |
| Primera, 30 min + pico (no reproducible) | 67 | 1.919 / 2.052 MiB | kafka 696, transfer-service 450, postgres 343, risk-scoring 297, risk-stream 284, rail-sim 33 |
| Segunda, 30 min + pico | 67 | 1.856 / 1.963 MiB | kafka 670, postgres 378, transfer-service 371, risk-stream 276, risk-scoring 270, rail-sim 38 |

Máximo medido: 2.052 MiB (≈ 2,0 GiB), con límites por contenedor que suman 4,4 GiB.

## c. `make fallback-demo`

Con `risk-scoring` parado, 500 filas nuevas del replay.

| Dato | Primera pasada (no reproducible) | Segunda pasada (con calentamiento) |
|---|---|---|
| 500 con el scoring parado (SQL) | `FALLBACK=500` | `FALLBACK=500` |
| Readiness tras reiniciar | 6 s | 6 s |
| Tanda 1 de 20 filas, tras el reinicio | `FALLBACK=3 MODEL=17` (otras corridas: 18 y 2; en una tanda de 100, 99 y 1) | `FALLBACK=1 MODEL=19` |
| Tanda 2 | `MODEL=20` | `MODEL=20` |

El tiempo hasta la primera decisión `MODEL` incluye la lectura del CSV de PaySim de cada tanda (≈ 25 s), así que tiene esa resolución: 29,1 s en la primera y 42,5 s en la segunda.

## d. `make aml-eval`

`amlgen` semilla 42 con la lista real del GAFI (`IR`, `KP`, `MM` en `HIGH`). 30.683 transferencias enviadas en 617 s (29.217 `201`, 1.466 `422` por `BLOCK`, 1.651 aprobadas en revisión). Informe completo: `docs/resultados/aml-eval.md` (el vigente es el de la segunda pasada).

| Tipología | Instancias | Detectadas, primera (lista desde 2026-10-30; no reproducible) | Detectadas, segunda (lista retroactiva) | Techo del manifest |
|---|---:|---:|---:|---:|
| `STRUCTURING` | 40 | 25 (62,50 %) | 25 (62,50 %) | 62,50 % |
| `COUNTRY_RISK` | 60 | **0 (0,00 %)** | 34 (56,67 %) | 65,00 % |
| `FAN_IN` | 20 | 0 (por diseño) | 0 (por diseño) | — |
| `FAST_PASS` | 30 | 0 (por diseño) | 0 (por diseño) | — |

Precisión: `STRUCTURING` 35 de 35. `COUNTRY_RISK` 0 alertas en la primera y 51 de 51 en la segunda. `THRESHOLD`: 51 alertas, 46 tocan alguna instancia. Alertas por 1.000 transferencias: 2,80 en la primera y 4,47 en la segunda. Ningún estado «inconsistente».

**Hallazgo (primera pasada): `COUNTRY_RISK` detectó 0 de 60.** `risk.country_risk` vale desde `list_date = 2026-10-30` y `amlgen` genera transferencias de enero de 2026; la regla usa el nivel vigente en `occurred_at` (D6) y no encontró ninguno. No era un fallo de la regla ni del generador, sino del tiempo de los datos de prueba.

**Segunda pasada.** Se cargó la lista con `make load-country-risk VALID_FROM=2026-01-01` (*lista aplicada retroactivamente al periodo simulado*, opción explícita; no se movió el periodo de `amlgen` ni se cambió ningún parámetro). El recall de `COUNTRY_RISK` queda en 56,67 %, por debajo del techo de 65 %: 26 instancias sin detectar. No se investigó.

## e. Conciliación

| Momento | `cuentas_descuadradas` / `retenciones_huerfanas` |
|---|---|
| Después del replay (primera y segunda pasada) | 0 y 0 |
| Después de `fallback-demo` y de reiniciar PostgreSQL (primera pasada) | 0 y 0 |
| Después del aml-eval (segunda pasada) | 0 y 0 |

## Ajustes heredados de 4.C

| Ajuste | Verificación |
|---|---|
| 8a `statement_timeout = '250ms'` en `risk_svc` | `db/tests/test_permissions.py`: el rol lo tiene, `pg_sleep(1)` se cancela, los otros roles no lo tienen y `make db-grants` es idempotente. `make test-risk` (90 pruebas, incluidas las del rol `stream`) pasa con el `grants.sql` real |
| 8b pool de `risk-scoring` | Vert.x no tiene tamaño mínimo: `ScoringStartup` abre las 10 conexiones y `idle-timeout=PT0S` evita que se cierren |
| 8c reiniciar PostgreSQL con `risk-scoring` arriba | Sondeo cada 0,5 s: un `500` a 1,28 s y `200` desde 1,85 s (0,6 s después de volver PostgreSQL). Los tres servicios siguen con 0 reinicios y `healthy`; una tanda posterior de 50 filas sale 50 de 50 `MODEL` |

## Hallazgos y su estado

1. **Arranque en frío del scoring (505 `FALLBACK`).** Corregido: `risk-scoring` recorre 200 veces la ruta completa antes de declararse listo (el `INSERT` en una transacción que se revierte; 0 filas en `risk.scoring_log`) y `transfer-service` calienta también la ruta POST y reintenta hasta que `risk-scoring` responda. Resultado: 3 `FALLBACK` (ver a).
2. **Errores del 0,02 % al 0,09 % en el scoring.** Consultas a PostgreSQL por encima de 100 ms; en la segunda pasada, una sola ráfaga de 0,3 s (ver b). Sin investigar.
3. **`COUNTRY_RISK` 0 de 60.** Causa y segunda pasada en d.
4. **Desajuste de historia (D1), medido.** Ver la matriz y la tabla de features en a.
5. **Fallo propio de 4.F, corregido.** El reintento del calentamiento usaba `ON CONFLICT (transfer_id)`, que exige `SELECT`; `sim_loader` solo tiene `INSERT`. Corregido con pruebas en `simulator/` y `db/tests`.
