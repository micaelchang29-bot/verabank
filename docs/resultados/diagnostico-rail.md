# Diagnóstico del rail (5.G.2)

Fecha: 2026-11-19, 03:15–04:05 (UTC−5). Equipo: laptop de referencia, 12 núcleos y 31 GB en Windows con WSL2 y Docker
Desktop (15,5 GB visibles en WSL). Base limpia (`make reset-data CONFIRM=yes && make up`, reset 4 de 10 del cierre).
Cargas con `make load-transfers RATE=<tps> DURATION=5m` (k6, 60 s de calentamiento sin umbrales + 5 min medidos; 70 %
EXTERNAL; rail con 10 % de rechazos; después settle, `reconcile-full` y `verify-audit`). Muestreo cada 10 s con
`perf/diag_rail.sh`; resumen con `perf/diag_summary.py`; datos crudos en `data/perf/diag/` (no versionado).
`pg_stat_statements` solo durante el diagnóstico (override local de Compose, retirado al terminar).

## Mediciones

| Corrida | p95 HTTP | Transf./s procesadas | Retraso máx. `rail.replies.v1` (p0/p1/p2) | Drenaje tras la carga | Filas de auditoría/s | Espera media `pg_advisory_xact_lock(815001)` | Sesiones esperando 815001 (máx.) | `CreditStatusQuery` | CPU media PostgreSQL / transfer-service |
|---|---|---|---|---|---|---|---|---|---|
| base 50 TPS | 21,7 ms | 46,9 | 3 (1/2/1) | 21,7 s | 140,9 | 0,40 ms | 1 | 0 | 27,8 % / 40,6 % |
| base 100 TPS | 137,0 ms | 71,7 | 22.464 (9.110/7.850/6.245) | 141 s | 200,2 | 25,1 ms | 19 | 18.522 (1,11 por transferencia afectada) | 47,2 % / 47,5 % |
| E1: 100 TPS, `SPRING_KAFKA_LISTENER_CONCURRENCY=3` | 294,7 ms | 82,6 | 24.344 (8.281/8.033/8.030) | 78 s | 227,6 | 34,3 ms | 19 | 15.124 | 53,6 % / 61,2 % |
| E2: 100 TPS, `synchronous_commit=off` | 8,7 ms | 97,6 | 6 | 0,2 s | 270,9 | 0,06 ms | 0 | 0 | 59,4 % / 69,2 % |

Otras cifras de la base de 100 TPS: 911 commits/s; 2,78 filas de auditoría por saga (TRANSFER_CREATED, PROCESSING,
COMPLETED o FAILED); `saga.stuck` 0; `outbox.lag` máx. 1,6 s; 513 decisiones por fallback de scoring; memoria máxima
2,16 GiB. Las dos sentencias más costosas de `pg_stat_statements` en la base de 100 TPS son el `INSERT` en
`audit.audit_log` (99.916 llamadas, 2.520 s acumulados, 25,2 ms de media) y `SELECT pg_advisory_xact_lock(815001)`
(99.916 llamadas, 2.509 s, 25,1 ms de media); la siguiente suma 10 s. `pg_test_fsync` en el volumen de datos:
`fdatasync` 2,04 ms por operación (489 op/s; es el método por defecto y el más rápido), `fsync` 4,44 ms.

## Hipótesis

| # | Hipótesis | Experimento (una variable, revertido) | Resultado | Veredicto |
|---|---|---|---|---|
| H1 | Consumidor secuencial: un hilo para las 3 particiones de `rail.replies.v1` | E1: 3 hilos de consumo | +15 % de transferencias/s y drenaje 45 % más corto, pero p95 ×2,2 y la espera en 815001 sube de 25 a 34 ms: el trabajo paralelo se forma en el mismo bloqueo | Secundaria: no es el techo |
| H2 | Serialización por la cadena de auditoría (`pg_advisory_xact_lock(815001)` en el trigger, retenido hasta el commit) | E2: commit sin esperar el `fsync` del WAL | La espera en 815001 cae de 25 ms a 0,06 ms, el retraso de la cola de 22.464 a 6 y el sistema procesa lo ofrecido | **Causa** (junto con el `fsync`) |
| H3 | Bucle de `CreditStatusQuery` (sagas que pasan los 30 s de `rail-reply-timeout`) | Sin experimento propio: se observa en las tres corridas | 0 a 50 TPS y con E2; 18.522 a 100 TPS cuando las respuestas tardan más de 30 s en procesarse | Consecuencia: amplifica la cola, no la crea |
| H4 | PostgreSQL como cuello (CPU) | E2 y la CPU medida | PostgreSQL al 47 % de CPU media en la base de 100 TPS; con E2 procesa más con 59 %: no está saturado de CPU, sino serializado en I/O | Rechazada como CPU; el límite es la latencia de `fsync` bajo el bloqueo |

## Causa

Cada transacción que escribe auditoría (2,78 por transferencia) toma `pg_advisory_xact_lock(815001)` en el trigger de
la cadena y lo retiene hasta el commit, incluido el `fsync` del WAL. En este equipo ese tramo dura unos 4–5 ms (resto de
la transacción más un `fdatasync` de ~2 ms), así que la cadena admite como mucho unas 200–230 transacciones con
auditoría por segundo, ≈ 72–82 transferencias/s. Por encima de ese ritmo, las respuestas del rail se acumulan en
`rail.replies.v1`; cuando una saga pasa los 30 s sin procesar su respuesta emite `CreditStatusQuery`, que agrega
mensajes y transacciones a la misma cola. A 50 TPS (el objetivo de T-17) el sistema va holgado: p95 21,7 ms y retraso 3.

## Propuestas (por impacto y costo)

| Propuesta | Impacto esperado | Costo | Tipo | Estado |
|---|---|---|---|---|
| Auditoría por lotes con un escritor asíncrono (§8, decisión 6): un solo `fsync` y un solo bloqueo por lote | Alto: quita el bloqueo del camino de cada saga | Medio-alto: cambio de código y de la garantía «auditoría en la misma transacción» | Diseño | **Decisión del usuario** |
| Tomar el bloqueo de la cadena lo más tarde posible ya se hace (la auditoría es la última sentencia de cada transacción); un orden global por secuencia y el encadenado del hash en segundo plano lo quitaría del commit | Alto | Medio: cambio de DDL del trigger y del verificador | Diseño (DDL) | **Decisión del usuario** |
| Almacenamiento con `fsync` más rápido en producción (NVMe local, no el VHD de WSL2) | Medio-alto: el techo escala con 1/latencia de `fsync` | Bajo en producción; no aplica a la laptop | Entorno | Recomendación |
| `SPRING_KAFKA_LISTENER_CONCURRENCY=3` | Bajo: +15 % de volumen con peor p95 a 100 TPS | Bajo | Configuración | **No aplicado**: no cambia el techo y empeora la latencia en el pico |
| Subir `rail-reply-timeout` de 30 s | Bajo: solo recorta los `CreditStatusQuery` | Bajo | Plazo de la saga | **Decisión del usuario** (reservado en el plan) |
| `synchronous_commit=off` | Alto | Pierde durabilidad de los últimos commits ante una caída | Configuración | **Rechazado**: inaceptable para el libro contable |
| `wal_sync_method` | Nulo: `fdatasync` ya es el más rápido según `pg_test_fsync` | — | Configuración | Descartado |

No se aplicó ningún arreglo: los de configuración no mueven el techo y los que lo mueven son decisiones de diseño. T-17
(50 TPS, pico de 100 TPS durante 5 min) puede correr sin cambios; el pico deja cola que se drena después.

## Incidentes

Durante el diagnóstico el equipo se colgó dos veces (pantallazo azul `0x139` y `0x119`, este último del driver de la
GPU AMD, a unos 15 min de apagarse la pantalla). E1 se repitió entero y
la base se verificó sin descuadres.
