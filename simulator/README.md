# verabank-sim

Simuladores de VeraBank en Python 3.12 (`uv`). Hoy solo existe el subcomando `rail`; el
despachador (`verabank_sim/cli.py`, tabla `COMMANDS`) queda listo para añadir `chaos`, `replay` y
`aml`.

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

## Pruebas

`uv run pytest` ejecuta las unitarias (el motor con reloj falso, sin Kafka). La integración lleva la
marca `integration`, se pide con `-m integration` y **falla** si no hay Kafka: no se omite en
silencio. Usa un grupo de consumo propio y lo borra al terminar.
