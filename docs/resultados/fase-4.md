# Resultados de la fase 4

Mediciones propias de la corrida de aceptación del 2026-11-17, en la laptop de referencia (WSL2, 12 núcleos visibles, 15 GB). Todo en Compose con `make up`. La columna «objetivo» es la de `docs/diseno.md` §1.2; no es un resultado. Las cifras salen de `data/replay/summary.json`, `data/perf/` y las salidas de `make`, sin estimaciones.

## a. Replay de PaySim

| Dato | Valor |
|---|---|
| Comando | `make up && make replay N=20000 TPS=50` |
| Parámetros | partición de prueba, pasos 575 a 590, `AMOUNT_FACTOR=1`, calentamiento de 26.503 filas (`COPY`), 20.000 cuentas sembradas |
| Enviadas / duración / TPS | 20.000 / 405,8 s / 49,28 |
| Código HTTP | `201`: 19.873; `422` (`transfer-declined`, `BLOCK`): 127 |
| `fraud_decision` | `APPROVE` 19.167, `REVIEW` 706, `BLOCK` 127 |
| `decision_source` | `MODEL` 19.495, `FALLBACK` 505 (ver hallazgo 1) |
| Estado final | `COMPLETED` 19.167, `IN_REVIEW` 706, `REJECTED` 127 |
| Latencia de `POST /transfers` (cliente) | p50 23,7 ms, p95 57,0 ms, p99 130,4 ms. Objetivo: p95 < 400 ms |
| `scoring_latency_ms` | p50 6, p95 13, p99 21 |
| Etiquetas publicadas | 20.000 |
| `make reconcile` después | `cuentas_descuadradas = 0`, `retenciones_huerfanas = 0` |

## b. Scoring con k6 (`POST /internal/v1/scores` directo a `risk-scoring`)

Comando: `make perf-scoring` (5 min) y `DURATION=30m PEAK=1 make perf-scoring`. Calentamiento de 60 s sin umbrales. Pool de 10 conexiones, `query-timeout` de 100 ms.

| Corrida | Carga | Solicitudes | p95 | p99 | Errores | Objetivo |
|---|---|---:|---:|---:|---:|---|
| 5 min | 50 TPS constantes | 15.000 | 6,09 ms | 8,82 ms | 14 (0,09 %) | p95 < 150 ms, p99 < 300 ms, errores < 1 % |
| 30 min | 50 TPS constantes | 90.000 | 6,39 ms | 8,61 ms | 25 (0,03 %) | idem; 50 TPS durante 30 min |
| Pico | 100 TPS durante 5 min tras los 30 | 30.000 | 6,89 ms | 10,45 ms | 0 (0,00 %) | pico de 100 TPS 5 min |

Todos los umbrales de k6 pasaron. Los errores son respuestas `500 internal-error`: el límite de 100 ms por consulta a PostgreSQL vencido (la latencia máxima observada fue 191 ms). Hallazgo 2.

**Memoria** (`docker stats --no-stream`, una muestra cada 30 s durante la carga; suma de los seis contenedores):

| Corrida | Muestras | Total mín. / máx. | Por servicio, máximo |
|---|---:|---|---|
| 5 min | 11 | 1.925 / 1.942 MiB | kafka 557, transfer-service 449, postgres 342, risk-scoring 284, risk-stream 282, rail-sim 32 |
| 30 min + pico | 67 | 1.919 / 2.052 MiB | kafka 696, transfer-service 450, postgres 343, risk-scoring 297, risk-stream 284, rail-sim 33 |

Objetivo de recursos: Compose ≤ 8 GB. Máximo medido, 2.052 MiB (≈ 2,0 GiB), con límites por contenedor que suman 4,4 GiB.

## c. `make fallback-demo`

Con `risk-scoring` parado, 500 filas nuevas del replay (`START_ROW=23000`).

| Paso | Resultado |
|---|---|
| 500 con el scoring parado | SQL: `decision_source`: `FALLBACK=500` (`REVIEW` 494 y `APPROVE` 6 en una de las corridas: el fallback aprueba hasta 500 PEN) |
| Readiness tras reiniciar | 6 s |
| Tanda 1 de 20 filas, 32 s después de pedir el reinicio | `FALLBACK=3 MODEL=17` |
| Tanda 2, 63 s después | `MODEL=20`; el circuito cerró y el script termina bien |

Las tandas incluyen la lectura de PaySim (≈ 25 s), así que los tiempos tienen esa resolución. En tres corridas la primera tanda tras el reinicio salió con `FALLBACK` 99 de 100, 18 de 20 y 3 de 20: el scoring recién reiniciado responde lento a sus primeras llamadas (hallazgo 1). El script rechaza cualquier resultado distinto de 500 `FALLBACK` y de una tanda final 100 % `MODEL`.

## d. `make aml-eval`

Informe completo: `docs/resultados/aml-eval.md`. `amlgen` semilla 42 con la lista real del GAFI; 30.683 transferencias enviadas (29.217 `201`, 1.466 `422` por `BLOCK`, 1.651 aprobadas en revisión) en 617,5 s.

| Tipología | Instancias | Detectadas | Recall | Techo del manifest |
|---|---:|---:|---:|---:|
| `STRUCTURING` | 40 | 25 | 62,50 % | 62,50 % |
| `COUNTRY_RISK` | 60 | 0 | 0,00 % | 65,00 % |
| `FAN_IN` | 20 | 0 | por diseño | — |
| `FAST_PASS` | 30 | 0 | por diseño | — |

Precisión: `STRUCTURING` 35 de 35. `THRESHOLD`: 51 alertas, 46 tocan alguna instancia. 2,80 alertas por 1.000 transferencias. Ningún estado «inconsistente». `COUNTRY_RISK` da 0 por una incompatibilidad de tiempos (hallazgo 3); no se ajustó nada.

## e. Conciliación

| Momento | Resultado |
|---|---|
| Después de (a) | 0 y 0 |
| Después de (c) | 0 y 0 (comprobado tras (d), que incluye a (c)) |
| Después de (d) y de reiniciar PostgreSQL (ajuste 8c) | 0 y 0 |

## Ajustes heredados de 4.C

| Ajuste | Verificación |
|---|---|
| 8a `statement_timeout = '250ms'` en `risk_svc` | `db/tests/test_permissions.py`: el rol lo tiene, `pg_sleep(1)` se cancela, los otros roles no lo tienen y `make db-grants` es idempotente. El rol `stream` usa las mismas pruebas de risk-service contra los permisos reales (`make test-risk`) |
| 8b pool de `risk-scoring` | Vert.x no tiene tamaño mínimo: `ScoringStartup` abre las 10 conexiones y `idle-timeout=PT0S` evita que se cierren. Con eso, las filas de k6 no pagan altas dentro del límite de 100 ms |
| 8c reiniciar PostgreSQL con `risk-scoring` arriba | Sondeo directo cada 0,5 s: una solicitud `500` a 1,28 s, `200` desde 1,85 s (0,6 s después de volver PostgreSQL). Los tres servicios mantienen 0 reinicios y siguen `healthy`; una tanda posterior de 50 filas sale 50 de 50 `MODEL` |

## Hallazgos

1. **Arranque en frío del scoring (505 `FALLBACK` de 20.000).** Las 505 decisiones `FALLBACK` del replay están en los primeros 10 s (11 s de tráfico: las primeras llamadas de `transfer-service` a `risk-scoring` superan el timeout de 120 ms, el circuito abre 10 s) y 3 sueltas después. Tras reiniciar `risk-scoring` ocurre igual: las primeras llamadas tardan más que 120 ms aunque la readiness ya sea verdadera. Propuesta, que cambia código y no he tocado: puntuar una solicitud sintética completa (features, ONNX, bitácora) antes de declarar la readiness, y calentar el cliente HTTP de `transfer-service`.
2. **Errores del 0,03 % al 0,09 % en el scoring.** Son consultas que superan el límite de 100 ms del cliente (`500 internal-error`) en una carga constante; con el modelo ya caliente y PostgreSQL al 5 % de CPU. No lo he investigado más; cumple el umbral (< 1 %). Para saber la causa haría falta medir `scoring.stage.duration{stage=features|log}` durante la carga.
3. **`COUNTRY_RISK` detecta 0 de 60.** `risk.country_risk` vale desde `list_date = 2026-10-30` (`valid_from` de la carga) y `amlgen` genera transferencias de enero de 2026: la regla usa el nivel vigente en `occurred_at` (D6) y no encuentra ninguno. No es un fallo de la regla ni del generador, sino de los datos de prueba. Opciones, a decidir por ti: cargar la lista con un `valid_from` anterior al periodo del generador; mover el periodo de `amlgen` a después del 30 de octubre de 2026; o dejarlo documentado.
4. **Un fallo propio de 4.F, corregido.** El reintento del calentamiento usaba `ON CONFLICT (transfer_id)`, que exige `SELECT`, y `sim_loader` solo tiene `INSERT`: el segundo replay fallaba con `permission denied`. Corregido con una prueba en `simulator/` y otra en `db/tests`.
