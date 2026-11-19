# Resultados de la fase 5

Solo cifras medidas en la laptop de referencia (12 núcleos, 31 GB, Windows con WSL2 y Docker Desktop; 15,5 GB visibles
en WSL). Los objetivos de §1.2 van en su columna, no como resultado. Fechas en hora local (UTC−5). Las corridas
destructivas tuvieron autorización explícita y contada (10 resets como máximo en el cierre).

## Arranque en frío del scoring (B; `make smoke` desde cero)

| Corrida | Fecha | Comando | Resultado | Latencia de la primera llamada de scoring | Objetivo |
|---|---|---|---|---|---|
| 1 | 2026-11-19 03:01 | `make reset-data CONFIRM=yes && make up && make smoke` | exit 0; primera transferencia `MODEL fraud-xgb/6` | 33 ms | `decision_source = MODEL`; < 120 ms (ADR-04) |
| 2 | 2026-11-19 03:03 | ídem | exit 0; `MODEL fraud-xgb/6` | 22 ms | ídem |
| 3 | 2026-11-19 03:04 | ídem | exit 0; `MODEL fraud-xgb/6` | 20 ms | ídem |

Prueba de integración `FirstCallLatencyIT` (cliente HTTP del JDK, primera llamada tras `ready`): antes 87 / 108 / 104 ms
(servicio 39–51 ms); después 52 / 53 / 52 ms (servicio 17–20 ms); sin calentamiento (mutación) 301 ms.

## Diagnóstico del rail (5.G.2)

Detalle en `docs/resultados/diagnostico-rail.md`. Resumen:

| Corrida | Fecha | Parámetros | p95 | Transf./s | Retraso máx. de `rail.replies.v1` | Espera media del bloqueo 815001 |
|---|---|---|---|---|---|---|
| base | 2026-11-19 03:16 | RATE=50, 5 min, rechazos 10 % | 21,7 ms | 46,9 | 3 | 0,40 ms |
| base | 2026-11-19 03:22 | RATE=100, 5 min | 137,0 ms | 71,7 | 22.464 | 25,1 ms |
| E1 | 2026-11-19 03:46 | RATE=100, consumidor con 3 hilos | 294,7 ms | 82,6 | 24.344 | 34,3 ms |
| E2 | 2026-11-19 03:57 | RATE=100, `synchronous_commit=off` (solo diagnóstico) | 8,7 ms | 97,6 | 6 | 0,06 ms |

Causa: la cadena de auditoría (`pg_advisory_xact_lock(815001)`, retenido hasta el commit) serializa un `fsync` de ~2 ms
(`pg_test_fsync`) por transacción con auditoría: techo de ≈ 200–230 por segundo, ≈ 72–82 transferencias/s.

## T-17 (carga) y reinicio de PostgreSQL

| Corrida | Fecha | Comando | Resultado | Objetivo (§1.2) |
|---|---|---|---|---|
| T-17 5 min | 2026-11-19 04:05 | `make reset-data CONFIRM=yes && make up && make load-transfers DURATION=5m` | 18.366 solicitudes (18.002 creadas, 364 reintentos idempotentes); p50 14,8 / p95 21,5 / p99 26,9 ms; 0 % fuera de 200/201/422; `saga.stuck` 0; `outbox.lag` máx. 0,28 s; `reconcile-full` 0; cadena íntegra (49.946 filas); memoria máx. 1,65 GiB | p95 < 400 ms; errores < 1 %; ≤ 8 GB |
| T-17 30 min con pico | 2026-11-19 04:12 | `make reset-data CONFIRM=yes && make up && make load-transfers DURATION=30m PEAK=1` | 110.181 solicitudes (108.002 creadas, 2.179 reintentos); p50 15,8 / p95 265,7 / p99 1.301,5 ms (máx. 2.623,6); 0 % fuera de 200/201/422; `saga.stuck` 0; `outbox.lag` máx. 5,1 s; fallback de scoring 9; drenaje 124,7 s; `reconcile-full` 0 (0 duplicados, 0 descuadres, 0 abiertas); cadena íntegra (298.989 filas); memoria máx. 2,46 GiB | ídem |
| Compensación de las FAILED (misma base) | 2026-11-19 04:48 | `make reconcile-full` | n = 7.347; p50 0,25 / p95 181,4 / p99 197,1 s; 77,79 % < 60 s | **99 % < 60 s: no cumple** (pico de 100 TPS sobre el techo del rail) |
| Reinicio de PostgreSQL | 2026-11-19 04:49 | `make pg-restart-test` | 450 solicitudes, 1 error (503); normalidad a los 0,4 s; 0 reinicios de contenedores | — |
| T-16 tras el reinicio | 2026-11-19 04:50 | `make verify-audit` | íntegra, 299.887 filas en 1,03 s | cadena íntegra |

## BI, razones y reentrenamiento

| Corrida | Fecha | Comando | Resultado | Objetivo |
|---|---|---|---|---|
| Siembra | 2026-11-19 04:51 | `make reset-data CONFIRM=yes && make up && make bi-seed STEPS=144 SETTLE_TIMEOUT=60m` | 124.001 filas (pasos 577–720) a 97,2 TPS; 50.029 filas de historia dirigida; 123.985 `MODEL`, 16 `FALLBACK`; latencia de cliente p95 198,7 ms, scoring p95 8 ms; drenaje 1.673 s; 123.510 COMPLETED, 491 REJECTED, 1.192 revisiones aprobadas | — |
| BI | 2026-11-19 05:44 | `make bi-up && make bi-check` | 7 tarjetas; KPI 1: 4 días maduros (25-01: 239 fraudes, 49,37 % detectados, 68,03 % del monto; 26-01: 271, 46,49 %, 69,02 %; 27-01: 278, 44,60 %, 82,10 %); KPI 2: `fraud-xgb/6` FPR 1,158 %, precisión 36,61 %, 14 legítimas bloqueadas; KPI 3: THRESHOLD 88.672 alertas, p50 0,031 s, p95 0,087 s; KPI 4: aprobación automática 98,59–98,74 % por segmento; KPI 5: p95 de scoring 8 ms, fallback 0,00–0,02 %; EXTRA B: 93 casos | KPI 1 y 2 con filas maduras |
| Razones | 2026-11-19 05:45 | `make ml-reasons` ×2 | 1.667 casos explicados (5.001 filas, 3 por caso) en 3,8 s; segunda ejecución: ninguno | idempotente |
| PSI | 2026-11-19 05:45 | `psi --reference valid` y `--reference train` | ventanas 577–696: score contra `valid` 0,018–0,116, sin alertas; contra `train` 0,37–0,44 (informativa) | sin alerta sin deriva |
| Disparadores | 2026-11-19 05:45 | `make retrain-check` | drift no; recall 0,4686 frente a 0,4781 (caída 0,95 puntos, umbral 5); 13,29 días simulados (umbral 30): ninguno | — |
| Reentrenamiento | 2026-11-19 05:46 | `make retrain REASON=manual` | challenger `fraud-xgb/8`: PR-AUC 0,6300 frente a 0,5221 del champion en la misma validación interna (`ml/reports/retrain-report.md`) | — |
| Promoción | 2026-11-19 05:46 | `make promote` / `make rollback` | se niega: 0 pasos en sombra con etiqueta madura (mínimo 24); rollback: sin `previous_champion` | **no verificable con esta partición** (ver abajo) |

Límite de promote/rollback: verificarlos exige reconstruir risk-scoring con el challenger, ≥ 24 pasos de tráfico en
sombra (721–744) y 48 pasos más para que maduren sus etiquetas (hasta el paso 792); la partición de prueba de PaySim
termina en el 744. La lógica está cubierta por `ml/tests/test_retrain.py`. Tras la corrida se restauraron los artefactos
versionados y el alias `challenger` (`fraud-logreg/6`); el informe del reentrenamiento se conserva.

Dashboard: http://localhost:3000/dashboard/2 (Metabase, perfil `bi`).

## kind (ampliación)

| Corrida | Fecha | Comando | Resultado | Objetivo |
|---|---|---|---|---|
| Herramientas | 2026-11-19 04:55 | descarga del release oficial | kind v0.33.0 (SHA-256 `aee61515…8e0d`, igual al publicado) y kubectl v1.36.1 oficial (`629d3f41…c2b7`; el de Docker Desktop no es el binario oficial), en `~/.local/bin` | binarios verificados |
| Clúster | 2026-11-19 05:47 | `make down && make kind-up` | creado en 33 s (nodo `kindest/node:v1.37.0` fijado por digest) | — |
| Despliegue 1 | 2026-11-19 05:47 | `make kind-deploy` | falló: transfer-service y risk-service en CrashLoopBackOff; el Service `postgres` inyectaba `POSTGRES_PORT=tcp://…` (`docs/stop/20261119-0557-kind-service-links.md`) | — |
| Despliegue 2 | 2026-11-19 05:58 | `make kind-deploy` (con `enableServiceLinks: false`) | Jobs `db-migrate` y `bootstrap` completos; los 9 pods Ready en 78 s | todo Ready |
| Smoke | 2026-11-19 05:59 | `make smoke KIND=1` | verde | verde |
| Caos | 2026-11-19 06:04 | `make kind-chaos` | 4.906 respuestas; éxito (200, 201, 422) 100,00 %; p95 18,5 ms, p99 21,2 ms; FALLBACK 0 de 4.800; reemplazo del pod de risk-scoring Ready en 7 s | éxito ≥ 99 % |
| Memoria | 2026-11-19 06:08 | `docker stats verabank-control-plane` | 5,50 GiB reales (límites declarados 7,88 GiB) | ≤ 10 GiB |

Tras la corrida se borró el clúster y se volvió a levantar Compose con el BI (`make up && make bi-up`; volúmenes
conservados): dashboard en http://localhost:3000/dashboard/2.

## Incidentes resueltos durante el cierre

Registro completo en `docs/stop/` (un archivo por incidente con error, causa, solución y verificación).

## Cierre

| Paso | Fecha | Comando | Resultado |
|---|---|---|---|
| Firmas en mensajes | 2026-11-19 06:11 | `git log --format=%B \| grep -iE 'co-authored-by:\|generated with\|-session:'` | vacío |
| Firmas en el árbol | 2026-11-19 06:11 | `grep -rniE 'co-authored-by\|generated with' --exclude=verabank-manual.md --exclude-dir=.git --exclude-dir=.githooks .` | vacío salvo el propio comando en `docs/fase-5-prompts.md` (no versionado); `check-signatures --tree` limpio |
| Pull requests | 2026-11-19 06:11 | `gh pr list --state all` | no hay PR en el repositorio |
| CI | 2026-11-19 06:25 | ejecución 44512993745 (commit `ce2a08d`) | 9 de 9 jobs en verde, incluido el humo con Compose |
| Tag | 2026-11-19 | `git tag fase-5 && git push --tags` | sobre el commit de este documento (solo documentación respecto de `ce2a08d`) |

Resets usados en el cierre: 7 de 10 (3 smoke, 1 diagnóstico, 2 T-17, 1 BI).
