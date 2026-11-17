# verabank-sim

Simuladores de VeraBank en Python 3.12 (`uv`). Existen los subcomandos `rail` y `chaos`; el
despachador (`verabank_sim/cli.py`, tabla `COMMANDS`) queda listo para añadir `replay` y `aml`.

```bash
python -m verabank_sim <subcomando>      # dentro de simulator/: uv run python -m verabank_sim rail
```

## `rail`: rail interbancario simulado

Consume `rail.commands.v1` (grupo `rail-simulator`, commit manual) y responde en
`rail.replies.v1` con clave `transfer_id`. Es la contraparte de la saga externa
(`docs/saga.md`, §2, §4 y §5). No crea tópicos: hace falta el broker con sus tópicos
(`make up-core`).

### Uso

```bash
make rail-sim REJECT=0.5 DUP=0.1 DELAY=20:200 SILENT=0 LOST=0   # primer plano; Ctrl-C para cerrar
make test-rail                                                  # unitarias, sin Kafka
make test-rail-it                                               # integración, requiere Kafka arriba
```

Directamente: `cd simulator && uv run python -m verabank_sim rail --reject-rate 0.5 --seed 7`.

El broker se toma de `KAFKA_BOOTSTRAP_SERVERS`; si no está, de `localhost:$KAFKA_EXTERNAL_PORT`
(variable de entorno o `.env` de la raíz; por defecto 29092, el listener para el host).

### Parámetros

Cada bandera tiene su variable de entorno (la bandera manda). Los porcentajes son probabilidades
entre 0 y 1.

| Bandera | Variable | Defecto | Efecto |
|---|---|---|---|
| `--reject-rate` | `RAIL_REJECT_RATE` | `0.0` | Resuelve con `CreditRejected` (con `reason_code`) en vez de `CreditConfirmed` (con `rail_ref`) |
| `--delay-ms MIN:MAX` | `RAIL_DELAY_MS` | `20:200` | Retraso uniforme entre la instrucción y la respuesta; no bloquea el consumo |
| `--dup-rate` | `RAIL_DUP_RATE` | `0.0` | Emite la respuesta dos veces |
| `--dup-mode` | `RAIL_DUP_MODE` | `same-id` | `same-id` repite el mismo mensaje; `new-id` repite el payload con otro `event_id` |
| `--silent-rate` | `RAIL_SILENT_RATE` | `0.0` | El resultado se guarda pero la respuesta no se emite; la consulta lo revela |
| `--lost-rate` | `RAIL_LOST_RATE` | `0.0` | La instrucción se ignora y no deja estado; la consulta devuelve `UNKNOWN` |
| `--seed` | `RAIL_SEED` | `42` | Semilla: toda decisión sale de `Random(f"{seed}:{transfer_id}")` |
| `--state-db` | `RAIL_STATE_DB` | `simulator/.state/rail.db` | Base SQLite del estado (ignorada por Git) |
| `--ready-file RUTA` | — | — | Archivo que se crea al quedar suscrito, junto con la línea `READY` |
| `--bootstrap-servers` | `KAFKA_BOOTSTRAP_SERVERS` | ver arriba | Listener de Kafka para clientes en el host |
| `--group-id` | `RAIL_GROUP_ID` | `rail-simulator` | Grupo de consumo |
| `--offset-reset` | — | `earliest` | Desde dónde leer si el grupo no tiene offset confirmado |
| `--contracts-dir` | `VERABANK_CONTRACTS_DIR` | `contracts/events` | Esquemas con los que se valida |

Orden de decisión para una instrucción nueva: primero *lost*; si no, el resultado por
`reject-rate`; después *silent*. Misma semilla y mismo `transfer_id` dan el mismo resultado, también
tras una reentrega.

### Comandos (idempotentes por `transfer_id`)

| Comando | Respuesta |
|---|---|
| `CreditInstruction` | Nada de inmediato; la respuesta sale al vencer el retraso. Reentrega: si ya se emitió, la reemite idéntica (mismo `event_id`) sin retraso; si estaba silenciada, perdida o pendiente, no emite |
| `CreditStatusQuery` | `CreditStatus`: `CONFIRMED` o `REJECTED` si el resultado ya venció (una respuesta silenciada cuenta como vencida), `CANCELLED` si se canceló, `UNKNOWN` si sigue en curso, se perdió o nunca llegó |
| `CreditCancel` | Si no ha vencido, se perdió o nunca llegó: queda `CANCELLED` (también ignora una instrucción tardía) y responde `CANCELLED`. Si ya venció, responde el resultado real. Una cancelada nunca emite `CreditConfirmed` ni `CreditRejected` |

`event_id` de `CreditConfirmed` y `CreditRejected`: `uuid5(transfer_id, tipo)`, así un reenvío es el
mismo evento. `CreditStatus`: `uuid4` en cada respuesta. Cabeceras `event_id`, `event_type`,
`correlation_id`. Todo mensaje de salida se valida contra `contracts/events/` antes de publicarse.

Un comando que no valida va a `rail.commands.v1.dlt` (partición 0) con la causa en la cabecera
`dlt_cause` (y el origen en `dlt_source`), y se confirma.

### Operación

- Estado en SQLite; al reiniciar se reprograman los envíos pendientes. El offset se confirma solo
  después de persistir el estado y de entregar las respuestas inmediatas.
- Cada 10 s registra contadores: recibidas, confirmadas, rechazadas, silenciadas, perdidas,
  duplicadas (emisiones extra, incluida la reemisión ante una instrucción repetida), consultas y
  cancelaciones. «Confirmadas», «rechazadas» y «silenciadas» cuentan al decidir, no al emitir.
- SIGINT y SIGTERM cierran de forma ordenada: vacían el productor y confirman offsets. Los envíos
  aún no emitidos siguen en SQLite y salen en el siguiente arranque.

### Limitaciones

- **Estado local en SQLite y un solo proceso.** No hay réplicas ni estado compartido: dos
  instancias con la misma `--state-db` o el mismo grupo no son un caso soportado.
- Una respuesta se marca como emitida al entregarla al productor, antes de la confirmación del
  broker. Si el proceso muere justo entonces, esa respuesta se pierde: se comporta como una respuesta
  perdida y la consulta de estado la revela.
- El reloj de los retrasos es el de pared del proceso.

## `chaos` y `make chaos-rail`: caos del rail de extremo a extremo

`chaos` crea N transferencias EXTERNAL contra la API de transfer-service y comprueba lo que la API
promete bajo carga y con un rail que falla. `make chaos-rail` lo rodea del resto: servicio, rail
simulado y comprobaciones en la base y en Kafka. Corre solo en local; no está en la CI.

```bash
make chaos-rail REJECT=0.2 DUP=0.1 N=2000          # defaults de caos: SILENT=0.02 LOST=0.01 DELAY=20:300
make chaos-rail REJECT=0.2 DUP=0.1 DUP_MODE=new-id SILENT=0.05 LOST=0.02
make chaos-checks                                  # solo db/checks/chaos.sql, sobre lo que haya en la base
```

Requiere Docker (Postgres y Kafka del perfil core), Java 21, `uv` y `.env` con `VERABANK_API_KEYS`.

### Parámetros de `make chaos-rail`

| Variable | Defecto | Efecto |
|---|---|---|
| `REJECT` | `0` | `--reject-rate` del rail; con `LOST` fija el % de `FAILED` esperado (ver `chaos`) |
| `DUP` | `0` | `--dup-rate` del rail |
| `DUP_MODE` | `same-id` | `same-id` o `new-id` (ver `rail`) |
| `SILENT` | `0.02` | `--silent-rate` del rail |
| `LOST` | `0.01` | `--lost-rate` del rail; también se pasa a `chaos` para calcular el `FAILED` esperado |
| `DELAY` | `20:300` | `--delay-ms` del rail |
| `N` | `2000` | Transferencias a crear |

Los defaults de `SILENT`, `LOST` y `DELAY` son los del caos y difieren de los de `make rail-sim`.
`CONCURRENCY` (20) y `CHAOS_TIMEOUT` (180 s) se pueden fijar por entorno.

### Qué hace, en orden

1. Arranca o reinicia transfer-service (`scripts/up-core.sh`, que también carga la semilla) con
   `SAGA_RAIL_REPLY_TIMEOUT=5s` y `SAGA_POLL_BACKOFF_BASE=2s`, para que los casos de rail callado
   terminen en segundos (consulta a los 5 s, cancelación hacia los 19 s). **El servicio queda
   corriendo con esos tiempos**; `make up-core` lo reinicia con los de siempre.
2. Arranca `rail-sim` en segundo plano con `--ready-file` (estado SQLite propio, borrado en cada
   corrida), espera a que esté listo y lo detiene al terminar, también si algo falla (`trap`).
3. Ejecuta `chaos`, y después `make reconcile`, `make check-events` y `db/checks/chaos.sql`. Corren
   las cuatro etapas aunque una falle; el objetivo termina con código distinto de 0 si cualquiera falló.

### `chaos`

```bash
cd simulator && uv run python -m verabank_sim chaos --n 2000 --concurrency 20 --timeout 180 --reject-rate 0.2 --lost-rate 0.01
```

La clave de API sale de la primera entrada de `VERABANK_API_KEYS` del entorno (`make chaos-rail` la
exporta desde `.env`); no se imprime ni se registra. La URL es `http://localhost:$SERVER_PORT`
(`--base-url`). Otras banderas: `--lost-rate` (0.0), `--replay-rate` (0.05), `--reuse-rate` (0.01) y `--seed`.

- **Orígenes y montos.** Cuentas semilla PREMIUM y PYME (19 a 30), montos de 1.00 a 10.00. Antes de
  empezar lee el saldo disponible (`GET /api/v1/accounts/{id}`) y reparte las solicitudes sin
  comprometer más del 90 % de ninguna cuenta, suponiendo que todas se completan. La semilla actual
  alcanza para 2000 transferencias por corrida; si algún día no alcanzara, termina con código 2 y
  pide una semilla adicional (con el saldo como transferencia desde CLEARING y sus asientos).
- **Idempotencia.** Cada solicitud lleva una `Idempotency-Key` única. El 5 % se reenvía idéntica
  tras la respuesta original: debe dar 200, `Idempotent-Replayed: true` y el mismo `transfer_id`. El
  1 % (disjunto del anterior) reutiliza la clave con otro monto: debe dar 422 `idempotency-key-reuse`.
- **Espera.** Consulta `GET /api/v1/transfers/{id}` hasta que todas estén en `COMPLETED`, `FAILED` o
  `REJECTED`, o venza `--timeout` (contado desde el inicio).
- **Éxito (código 0).** Todas las creaciones con 201; reenvíos y reusos como se esperan; ninguna
  transferencia sin estado final; ninguna `REJECTED` (nada debe fallar por saldo ni por riesgo); y
  `FAILED` cerca del esperado. Los rechazos pedidos, las instrucciones perdidas que acaban canceladas y
  los silencios que revelan un rechazo producen `FAILED`. Esperado = `REJECT`·(1−`LOST`) + `LOST`
  (los rechazos se reparten entre las instrucciones que sí llegan al rail). Falla si
  |observado − esperado| > máx(1 punto, 4·√(p(1−p)/N)), con p el esperado y N las transferencias:
  la holgura sigue a N y a `LOST` en vez de ser fija (con N=2000, `REJECT` 0.2 y `LOST` 0.01 son
  ±3.63 puntos; con `REJECT` 0 y `LOST` 0.05, ±1.95).

### Comprobaciones posteriores

| Comprobación | Falla si |
|---|---|
| `make reconcile` | `cuentas_descuadradas` o `retenciones_huerfanas` ≠ 0 |
| `make check-events` | una `COMPLETED` no tiene `TransferCompleted`, o una `FAILED` no tiene `TransferFailed` con `compensated = true` en `transfer.events.v1` |
| `db/checks/chaos.sql` (por `scripts/chaos-checks.sh`, con psql como `reconcile`) | hay sagas que no son `COMPLETED` ni `COMPENSATED`; una `COMPLETED` externa no tiene exactamente un asiento D en el origen y uno C en CLEARING; una `FAILED` tiene asientos; hay asientos duplicados por transferencia, cuenta, sentido y tipo; o el p99 de `saga_state.updated_at - payload.compensation_started_at` en las `COMPENSATED` por fallo del rail supera 60 s |

Las comprobaciones de base y Kafka miran toda la base, no solo la última corrida: una saga
atascada de antes también las hace fallar.

## Pruebas

`uv run pytest` ejecuta las unitarias (el motor con reloj falso, y `chaos` con un cliente HTTP falso; sin Kafka ni servicios). La integración lleva la
marca `integration`, se pide con `-m integration` y **falla** si no hay Kafka: no se omite en
silencio. Usa un grupo de consumo propio y lo borra al terminar.
