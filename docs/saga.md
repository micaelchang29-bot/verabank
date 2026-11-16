# Saga de transferencias

Especificación del orquestador de `transfer-service`. Desarrolla §2.3 y ADR-01 de `docs/diseno.md` y es la fuente de verdad de estados, transiciones, temporizadores, mensajería y métricas de la saga. No cambia el DDL ni los esquemas de eventos.

## 1. Principios

1. Las transferencias internas son un paso local y atómico de la misma máquina de estados (ADR-01). La saga se justifica por el tramo externo.
2. Ninguna llamada de red ocurre dentro de una transacción. El scorer se invoca entre `tx1` y `tx2`; el rail solo se alcanza por outbox.
3. Un silencio o un timeout del rail no es un fallo. Solo se compensa con una respuesta explícita del rail: `CreditRejected`, o `CreditStatus` con `REJECTED` o `CANCELLED`.
4. Cada transición, su evento de outbox, su fila de auditoría y, si la causa es un mensaje, su registro en el inbox se confirman en una sola transacción.
5. Concurrencia: bloqueo optimista por `core.saga_state.version`. La unicidad de asientos (`uq_entry`) es la última defensa contra un doble asiento.
6. Los fondos no se pierden ni se duplican: ante un fallo que la saga no puede resolver, quedan retenidos y la saga se detiene con alerta (`COMPENSATION_FAILED`).

## 2. Estados y transiciones

`transfers.status` conserva su valor mientras exista una retención; solo pasa a un estado final al liberarla o consumirla. Por eso `make reconcile` no ve retenciones huérfanas aunque la saga se detenga.

| `saga_state.status` | `current_step` | `transfers.status` | Significado | `wake_at` |
|---|---|---|---|---|
| `RUNNING` | `FUNDS_HELD` | `PENDING` | Retención hecha; falta el score o cerrar `tx2` | Vencimiento del arrendamiento: `now + saga.resume-after` |
| `IN_REVIEW` | `IN_REVIEW` | `IN_REVIEW` | Espera al analista | `NULL` |
| `WAITING_REPLY` | `CREDIT_REQUESTED` | `PROCESSING` | Instrucción emitida; espera respuesta del rail | Próximo sondeo al rail |
| `WAITING_REPLY` | `CANCEL_REQUESTED` | `PROCESSING` | Cancelación pedida; espera respuesta | Próxima cancelación |
| `COMPENSATING` | `COMPENSATING` | `PROCESSING` | El rail rechazó o canceló; falta liberar la retención | Próximo reintento |
| `COMPLETED` | `DONE` | `COMPLETED` | Asientos hechos | `NULL` |
| `COMPENSATED` | `DONE` | `FAILED` o `REJECTED` | Retención liberada | `NULL` |
| `COMPENSATION_FAILED` | El paso donde se detuvo | Sin cambio | Requiere operador; fondos retenidos | `NULL` |

| Id | Disparador | Efecto (una transacción) | Estado siguiente |
|---|---|---|---|
| S1 | Solicitud válida | `tx1`: transferencia `PENDING`, retención, saga con `wake_at = now + saga.resume-after`, outbox `TransferInitiated`, auditoría | `RUNNING` / `FUNDS_HELD` |
| S2 | `tx2` con `BLOCK` | Liberar retención; `REJECTED` (`RISK_BLOCK`); outbox `TransferScored` y `TransferRejected` | `COMPENSATED` / `DONE` |
| S3 | `tx2` con `REVIEW` | `IN_REVIEW`; outbox `TransferScored` y `TransferHeld`; `wake_at = NULL` | `IN_REVIEW` |
| S4 | `tx2` con `APPROVE`, rail `INTERNAL` | Asientos D/C; `COMPLETED`; outbox `TransferScored` y `TransferCompleted` | `COMPLETED` / `DONE` |
| S5 | `tx2` con `APPROVE`, rail `EXTERNAL` | `PROCESSING`; outbox `TransferScored` y `CreditInstruction`; `wake_at = now + saga.rail-reply-timeout` | `WAITING_REPLY` / `CREDIT_REQUESTED` |
| S6 | Analista `APPROVE` sobre `IN_REVIEW` | Igual que S4 o S5, más auditoría `TRANSFER_REVIEWED` | S4 o S5 |
| S7 | Analista `REJECT` sobre `IN_REVIEW` | Liberar retención; `REJECTED` (`ANALYST_REJECT`); outbox `TransferRejected`; auditoría `TRANSFER_REVIEWED` | `COMPENSATED` / `DONE` |
| S8 | `CreditConfirmed`, o `CreditStatus` `CONFIRMED` | Asiento D en origen y C en la cuenta `CLEARING`; consumir la retención; `COMPLETED`; outbox `TransferCompleted` | `COMPLETED` / `DONE` |
| S9 | `CreditRejected`, o `CreditStatus` `REJECTED` o `CANCELLED` | Registrar `payload.compensation_started_at` y `payload.fail_reason` (`RAIL_REJECTED` o `RAIL_CANCELLED`); `wake_at = now` | `COMPENSATING` |
| S10 | Inmediatamente tras S9, en una transacción propia; o por el sondeo | Liberar retención; `FAILED` con `failure_reason`; outbox `TransferFailed` con `compensated = true` | `COMPENSATED` / `DONE` |
| S11 | `wake_at` vencido en `CREDIT_REQUESTED` y `polls < saga.cancel-after-polls` | Outbox `CreditStatusQuery`; `polls + 1`; `wake_at = now + min(base · 2^(polls−1), max)` con `polls` ya incrementado | Sin cambio |
| S12 | `wake_at` vencido en `CREDIT_REQUESTED` y `polls >= saga.cancel-after-polls` | Outbox `CreditCancel`; `cancels = 1`; `wake_at = now + base` | `WAITING_REPLY` / `CANCEL_REQUESTED` |
| S13 | `wake_at` vencido en `CANCEL_REQUESTED` | Outbox `CreditCancel`; `cancels + 1`; `wake_at = now + min(base · 2^(cancels−1), max)` | Sin cambio |
| S14 | Fallo local al ejecutar cualquier paso (base de datos, no el rail) | En transacción aparte: `attempt + 1`, `last_error`, `wake_at` por backoff local; la transacción del paso se revierte | Sin cambio |
| S15 | S14 con `attempt = saga.max-attempts` | `COMPENSATION_FAILED`; `payload.resume_status` = estado previo; alerta (§8) | `COMPENSATION_FAILED` |
| S16 | Reanudación operativa (§8) | `status = payload.resume_status`; `attempt = 0`; `wake_at = now`; auditoría `SAGA_RESUMED` | El estado previo |

`attempt` cuenta fallos locales consecutivos y vuelve a 0 con cada paso que termina bien. `polls` y `cancels` viven en `payload` y cuentan consultas y cancelaciones al rail; el rail callado no consume `attempt`.

S15 aplica a cualquier paso, no solo a la compensación: una saga cuyo cierre de `tx2` falla de forma permanente termina en `COMPENSATION_FAILED` con la retención intacta, en lugar de reintentarse sin fin.

## 3. Temporizadores y reintentos

Todos son configuración (`application.yml`, sobreescribible por variable de entorno, por ejemplo `SAGA_RAIL_REPLY_TIMEOUT`). Las pruebas usan valores cortos.

| Clave | Defecto | Uso |
|---|---|---|
| `saga.resume-after` | 30 s | Arrendamiento de una saga `RUNNING`; es el mismo plazo que hoy usa el reintento del cliente |
| `saga.rail-reply-timeout` | 30 s | Espera de la primera respuesta antes de consultar al rail |
| `saga.poll-backoff-base` / `saga.poll-backoff-max` | 10 s / 60 s | Espaciado de consultas y cancelaciones al rail |
| `saga.cancel-after-polls` | 3 | Consultas sin respuesta concluyente antes de pedir la cancelación |
| `saga.backoff-base` / `saga.backoff-max` | 1 s / 60 s | Backoff de fallos locales: `min(base · 2^(attempt−1), max)` |
| `saga.max-attempts` | 5 | Fallos locales antes de `COMPENSATION_FAILED` |
| `saga.poller-interval` / `saga.poller-batch` | 1 s / 50 | Frecuencia y tamaño del sondeo |
| `saga.stuck-after` | 15 min | Umbral de la métrica `saga.stuck` |

Cronología con los valores por defecto y un rail que no responde: consulta a los 30 s, 40 s y 60 s; `CreditCancel` a los 100 s. Solo `CANCELLED` o `REJECTED` del rail compensan; `CONFIRMED` completa.

## 4. Mensajería

- `rail.commands.v1` y `rail.replies.v1`: clave `transfer_id`, 3 particiones (§3.1). Cabeceras `event_id`, `event_type`, `correlation_id`.
- Consumidor de `rail.replies.v1`: grupo `transfer-service`, commit manual después de confirmar la transacción local.
- Inbox: `core.processed_messages` con consumidor `transfer-service:rail-replies`, insertado con `ON CONFLICT DO NOTHING` en la misma transacción que el efecto. Si no inserta fila, el mensaje ya se procesó: se confirma el offset sin efectos.
- Errores: un conflicto de versión se reintenta releyendo la saga. Otros fallos: 3 reintentos y luego `rail.replies.v1.dlt`. Ese tópico tiene una sola partición, así que el recuperador publica en la partición 0.
- Directo al DLT, sin reintentos: mensaje que no valida contra su esquema, `transfer_id` inexistente, transferencia `INTERNAL`, o respuesta de una saga a la que aún no se emitió instrucción.
- El rail puede reenviar el mismo mensaje (mismo `event_id`) o repetir el resultado con otro `event_id`: ambos casos se resuelven en §5.

## 5. Respuestas repetidas, tardías y contradictorias

| Situación | Tratamiento |
|---|---|
| Mismo `event_id` ya procesado | El inbox lo detecta; sin efectos; `inbox.duplicates` |
| Otro `event_id`, mismo sentido, saga ya en el estado final coherente (por ejemplo `CreditConfirmed` con la saga `COMPLETED`) | Sin cambios; `inbox.duplicates` |
| Rechazo o cancelación repetidos con la saga `COMPENSATING`, `COMPENSATED` o `COMPENSATION_FAILED` | Sin cambios |
| `CONFIRMED` con la saga `COMPENSATING`, `COMPENSATED` o `COMPENSATION_FAILED` | Contradicción: sin cambios de estado ni de asientos; registro ERROR y `saga.conflicting_reply`. Se concilia con el rail a mano |
| `REJECTED`, `CANCELLED` o `CreditRejected` con la saga `COMPLETED` | La misma contradicción |
| `CreditStatus` `UNKNOWN` | Se registra en el inbox; sin cambio de estado: manda el siguiente `wake_at` |

## 6. Revisión del analista

`POST /api/v1/transfers/{id}/review` con `{decision, comment}`.

- Solo sobre `IN_REVIEW`; en otro caso, 409 `review-not-pending`. Transferencia inexistente: 404 `transfer-not-found`.
- La transición se condiciona a `status = 'IN_REVIEW'` y a `version`: con dos analistas a la vez, uno recibe 200 y el otro 409.
- `APPROVE` reanuda como S4 o S5. `REJECT` es S7.
- Auditoría `TRANSFER_REVIEWED` en la misma transacción, con `decision` y `comment` en `detail`. El actor es el `client_id` del canal resuelto por la API key: el diseño solo prevé identidad por canal (§1.3).
- La saga en `IN_REVIEW` no tiene `wake_at` y no cuenta como detenida: espera a una persona.

## 7. Sondeo y rescate

Cada `saga.poller-interval`, el sondeo lee hasta `saga.poller-batch` sagas con `wake_at <= now()` y estado `RUNNING`, `WAITING_REPLY` o `COMPENSATING` (el índice parcial `ix_saga_wake`), con `FOR UPDATE SKIP LOCKED` y una transacción por saga.

| Estado y paso | Acción |
|---|---|
| `RUNNING` / `FUNDS_HELD` | Reanudar: reclamar la saga (transacción corta que empuja `wake_at` y sube `version`), pedir el score fuera de transacción (idempotente por `transfer_id`) y cerrar `tx2` (S2 a S5) con la versión reclamada |
| `WAITING_REPLY` / `CREDIT_REQUESTED` | S11 o S12 |
| `WAITING_REPLY` / `CANCEL_REQUESTED` | S13 |
| `COMPENSATING` | S10 |

El reintento del cliente con la misma `Idempotency-Key` y el sondeo comparten el mismo código de reanudación. La versión impide que ambos cierren `tx2`; el perdedor se descarta sin efectos.

## 8. Operación y métricas

Micrometer, visibles en `/actuator/metrics/<nombre>`.

| Métrica | Tipo | Definición |
|---|---|---|
| `saga.stuck` | Gauge, etiqueta `reason` | `compensation_failed`: sagas en ese estado. `no_progress`: sagas que no son finales ni `IN_REVIEW` y con `updated_at` más viejo que `saga.stuck-after`. Se calcula con una consulta programada cada 15 s. Mientras sea mayor que 0 se registra un ERROR con los ids |
| `saga.compensation.duration` | Timer | De recibir el rechazo o la cancelación (`payload.compensation_started_at`) a `COMPENSATED` |
| `saga.compensation_failed` | Counter | Cada paso a `COMPENSATION_FAILED` |
| `saga.conflicting_reply` | Counter | Contradicciones de §5 |
| `outbox.lag` | Timer, con histograma y objetivo de 1 s | `published_at − created_at`, al confirmar la publicación |
| `outbox.pending`, `outbox.oldest.age` | Gauges | Eventos sin publicar y edad en segundos del más viejo |
| `outbox.dlt`, `inbox.dlt`, `inbox.duplicates` | Counters | Eventos enviados al DLT y mensajes repetidos |

`updated_at` de `saga_state` se actualiza en cada transición.

**Reanudación operativa.** `POST /internal/v1/sagas/{id}/resume` (solo red interna, con API key) aplica S16 a una saga `COMPENSATION_FAILED`: 200 con el estado resultante, 404 `not-found` si no existe, 409 `saga-not-resumable` si está en otro estado. La reanudación es de lo que falló, no de la causa: si el fallo persiste, vuelve a `COMPENSATION_FAILED` tras `saga.max-attempts` intentos.

## 9. Cobertura de pruebas

| Prueba (§6) | Comportamiento que fija |
|---|---|
| T-05 | S9, S10 y `TransferFailed` con `compensated = true` |
| T-06 | S14 y S15 con backoff; fondos retenidos; S16 |
| T-07 | S11 con `CONFIRMED`; sin compensar a ciegas; cancelación solo tras consultas sin respuesta |
| T-08 | Inbox y §5: un solo par de asientos |
| T-09 | §7: el sondeo reanuda `RUNNING` / `FUNDS_HELD` sin depender del cliente |
| T-10 | Con Kafka caído la API acepta; el outbox se vacía en orden y `outbox.lag` crece y se recupera |

## 10. Pendientes

- **429 `rate-limited`.** El límite por canal del contrato (`429`, `Retry-After`) no está implementado: ninguna ruta responde 429 todavía. Lo cubrirá un filtro por `client_id` en una fase posterior; no afecta a la saga.
