# ADR-12 · Decisiones de la fase 4 (scoring, alertas AML, replay y evaluación)

- Contexto: la fase 4 construyó `risk-service` (rol `scoring` y rol `stream`, ADR-08), las reglas AML, el replay de PaySim y la evaluación de reglas. Al implementarla se tomaron doce decisiones que los ADR anteriores no cubren. Este ADR las registra, junto con los dos cambios de contrato que las acompañaron y los límites que se conocen. Las cifras de §1.2 son objetivos; los resultados medidos están en `docs/resultados/fase-4.md`.

Cada decisión sigue el orden contexto, decisión, consecuencias.

## Decisiones

### D1 · Historia del replay

- Contexto: las features de ventana (`src_out_count_24h`, `dst_in_count_24h`, …) leen `risk.scoring_log`. Un replay que parte de una tabla vacía deja las primeras 24 horas de negocio sin historia, y las features de arranque en frío no se parecen a las del entrenamiento.
- Decisión: antes de empezar, el replay carga en `risk.scoring_log` los 24 pasos previos a su primera fila, con el rol `sim_loader` y `COPY`, con `model_version = 'warmup'`. Las filas `warmup` cuentan como historia al calcular features. Se excluyen de PSI, de los KPIs y de cualquier entrenamiento.
- Consecuencias: las ventanas de 1 h y 24 h quedan correctas. `dst_seen_before`, `dst_age_hours` e `is_new_beneficiary` ven menos historia que en el entrenamiento (que las calculó sobre toda la partición anterior): es un desajuste aceptado y documentado, no un defecto por corregir. Quien lea `risk.scoring_log` debe filtrar `model_version <> 'warmup'`.

### D2 · Artefactos en el servicio

- Contexto: el servicio necesita `model.onnx`, `thresholds.json` y `manifest.json`; copiarlos al directorio del servicio crea dos fuentes de verdad.
- Decisión: `../ml/artifacts` es un directorio de recursos de `risk-service` en `pom.xml`, sin copias en el repositorio; viajan dentro del jar. El arranque verifica `model_sha256` del modelo contra `thresholds.json` y `manifest.json`, la versión del champion y que `feature_order` sea el conjunto que el código sabe calcular. Si algo no coincide, el servicio no queda listo (503 `model-not-loaded`).
- Consecuencias: reentrenar y exportar basta para cambiar el modelo servido, y no hay forma de desplegar un modelo que no corresponda a sus umbrales. El contexto de construcción de la imagen es la raíz del repositorio.

### D3 · Un servicio, dos despliegues

- Contexto: el scoring (ruta caliente, sin Kafka) y el procesamiento de eventos (consumidores, reglas AML, API de alertas) tienen perfiles de recursos y de fallo distintos, pero comparten dominio y esquema (ADR-08).
- Decisión: una imagen; el rol se elige al arrancar con `VERABANK_ROLE=scoring|stream`. Los beans de cada rol se activan por configuración de ejecución (`@LookupIfProperty`), no con `@IfBuildProfile`, para que la misma imagen sirva a ambos. El rol `scoring` no abre consumidores de Kafka y el rol `stream` no carga ONNX Runtime ni el modelo. Una prueba ArchUnit impide que los paquetes `scoring` y `stream` se importen entre sí.
- Consecuencias: una sola imagen que construir y versionar y dos despliegues que se escalan por separado. El coste es disciplina: cada bean nuevo debe declarar a qué rol pertenece.

### D4 · API de alertas

- Contexto: ADR-09 asigna las tablas de `risk` a `risk-service`; la API de alertas no debe ir en `transfer-service`.
- Decisión: la expone `risk-service` en el rol `stream`, con API key por canal como `transfer-service`. Cada cambio de estado se audita con `audit.append` (`SECURITY DEFINER`, `AML_ALERT_STATUS_CHANGED`, actor = canal). La función se añadió sin modificar el DDL del esquema `audit`: `risk_svc` solo recibe `EXECUTE` sobre ella y nunca acceso directo a las tablas.
- Consecuencias: quien cierra una alerta no puede alterar el rastro que deja. Hay un solo punto de escritura de alertas. La API vive en el rol `stream` (puerto 8082), no en el de scoring.

### D5 · Exención AML

- Contexto: la cuenta `CLEARING` recibe y entrega todo el dinero del sistema (la semilla, el replay) y dispararía alertas permanentes.
- Decisión: tabla `risk.aml_exempt_accounts`, sembrada con la cuenta `CLEARING` (`make seed-risk`, idempotente). `ExemptionRule` es la primera de la cadena y la corta; el hecho sí se proyecta en `risk.transfer_facts`.
- Consecuencias: la exención es un dato, auditable y modificable sin desplegar. Una cuenta exenta no genera alertas de ninguna regla, tampoco si más adelante se vuelve sospechosa.

### D6 · Alertas AML

- Contexto: las reglas deben ser deterministas sobre un flujo que puede reentregarse y reprocesarse, y las pruebas necesitan tiempo de negocio, no reloj.
- Decisión: la ventana es de negocio, `[t − 24 h, t]` con `occurred_at`, nunca `now()`. El fraccionamiento cuenta transferencias con `monto / umbral` en `[0,70, 1,00)` (máximo exclusivo). Su `dedupe_key` es regla, cuenta y `occurred_at` de la primera transferencia del conjunto; el upsert reemplaza `transfer_ids`, `total_amount` y `window_end` y conserva `created_at`, `status` y `trigger_emitted_at`. `AmlAlertRaised` se publica después de confirmar la transacción local, solo si la alerta es nueva o cambió su conjunto de transferencias, y sin outbox.
- Consecuencias: reprocesar el mismo evento no duplica alertas, y el resultado no depende de cuándo corra el servicio. La publicación sin outbox deja una ventana de pérdida (ver «Límites conocidos»).

### D7 · Score en el fallback

- Contexto: cuando `risk-scoring` no responde, `transfer-service` decide por monto (ADR-04). Un número inventado como score contaminaría PSI y el entrenamiento.
- Decisión: la decisión de respaldo guarda `fraud_score = NULL`, `model_version = 'fallback-v1'` y `decision_source = 'FALLBACK'`; `TransferScored` se emite sin `score`, y su esquema es condicional (ver «Cambios de contrato»).
- Consecuencias: ningún consumidor puede confundir una decisión de respaldo con una del modelo. Todo consumidor de `TransferScored` debe tolerar la ausencia de `score` cuando `decision_source` es `FALLBACK`.

### D8 · Modelo en sombra

- Contexto: antes de promover un modelo hay que ver cómo puntúa sobre tráfico real sin que afecte a ningún cliente.
- Decisión: el `challenger` se exporta junto con su `golden_challenger.jsonl`. Con `verabank.shadow.enabled` se puntúa después de responder y actualiza `shadow_score` solo si estaba vacío. Su fallo se cuenta en `scoring.shadow.failures` y nunca cambia la respuesta ni su latencia.
- Consecuencias: el challenger se compara sobre tráfico real a costa de CPU, fuera de la ruta crítica. Está desactivado por defecto (`VERABANK_SHADOW_ENABLED=false`).

### D9 · T-13 completo

- Contexto: el contrato de features debe cumplirse igual en Python (entrenamiento) y en Java (servicio); `golden.jsonl` cubre eventos de PaySim, no los casos límite.
- Decisión: `contracts/fixtures/feature_scenarios.json` contiene escenarios sintéticos pequeños (bordes de ventana `s-1`, `s-24` y `s-25`, eventos del mismo paso, hueco largo, montos extremos, los cuatro tipos) con las features esperadas de la implementación de referencia (`reference.py`, no la vectorizada) y el score esperado del champion en ONNX Runtime. Se genera con `make ml-fixtures`, sin datos ni servicios, y una prueba comprueba que el archivo está al día.
- Consecuencias: el servicio Java se prueba contra respuestas escritas a mano y contra el score real del modelo, no contra sí mismo. Cambiar el contrato de features obliga a regenerar `golden.jsonl` y los escenarios.

### D10 · Mapeo del replay

- Contexto: PaySim no tiene cuentas, clientes ni países; el replay debe pasar por la API real y conservar la conciliación.
- Decisión: una cuenta por nombre de PaySim (`uuid5`); saldo sembrado `max(oldbalanceOrg, suma de los montos del origen en el replay)`, con asientos desde `CLEARING` (así `make reconcile` sigue en 0 y 0); destinos `EXTERNAL` con `PS-<nombre>` y `dest_country = PE`; cliente por hash entre 30.000 identificadores sintéticos; `occurred_at = sim_epoch + (step − 1) h`.
- Consecuencias: el conjunto A no usa saldos, de modo que el saldo sembrado no altera el scoring. Todo el replay pasa por la saga externa y por eso necesita el rail simulado. Las filas con monto no positivo tras el factor no son transferencias válidas y se omiten.

### D11 · Orden del replay

- Contexto: las features del paso `s` solo deben ver pasos anteriores; enviar en paralelo filas de pasos distintos las contaminaría.
- Decisión: barrera por paso: no se envía ninguna fila del paso `s + 1` hasta que todas las del paso `s` tienen respuesta. Dentro del paso rige el ritmo de `TPS`, con concurrencia acotada.
- Consecuencias: el replay es reproducible. Su duración depende de la distribución de filas por paso y no solo de `N / TPS`.

### D12 · Evaluación AML

- Contexto: las reglas se miden contra la verdad de terreno de `amlgen` y no deben ajustarse para que las métricas salgan bien.
- Decisión: solo cuentan las alertas que tocan transferencias de `data/aml/`. `REVIEW` lo aprueba el simulador y `BLOCK` se informa aparte (instancias con transferencias rechazadas o sin completar). `FAN_IN` y `FAST_PASS` se informan como no cubiertas. `amlgen` usa la lista real del GAFI (`db/seed/country_risk.csv`, nivel `HIGH`). Si el recall de una tipología supera el techo del manifest, la evaluación lo marca «inconsistente» y se investiga. Ningún parámetro de regla ni del generador se modifica para mejorar una métrica.
- Consecuencias: las métricas son honestas y reproducibles (salida determinista y solo agregada), pero la cobertura global es baja a propósito. Una tipología sin regla no se «arregla» ajustando la evaluación.

## Cambios de contrato

1. **`TransferScored`, esquema condicional.** `score` es obligatorio salvo cuando `decision_source` es `FALLBACK` (D7); se expresa con `if`/`then`/`else` de JSON Schema 2020-12 en `contracts/events/TransferScored.v1.schema.json`. Con `MODEL` sigue siendo obligatorio.
2. **`openapi.yaml`, API de alertas.** `PATCH /api/v1/aml/alerts/{id}` devuelve `409 alert-transition-invalid` para una transición no permitida, y `GET /api/v1/aml/alerts` admite el filtro `rule_code`.

## Límites conocidos

- **Desajuste de historia (D1).** `dst_seen_before`, `dst_age_hours` e `is_new_beneficiary` ven menos historia en el replay que en el entrenamiento: las métricas de modelo del replay no son las de `test`.
- **Publicación sin outbox (D6).** `AmlAlertRaised` se publica después de confirmar la transacción. Si el proceso muere entre la confirmación y el envío, la alerta existe en la base y el mensaje no sale; reprocesar el evento de origen no lo republica (el inbox ya lo registró). El resto del sistema usa outbox (ADR-02); aquí se aceptó porque `aml.alerts.v1` aún no tiene consumidores.
- **`FAN_IN` y `FAST_PASS` sin cobertura (D12).** `FAN_IN` queda fuera del alcance de este corte y `FAST_PASS` no tiene regla en la tabla de §5a.
- **El resumen del replay lee `core.transfers`.** `decision_source` y `scoring_latency_ms` no los devuelve la API de lectura y el rol de BI no ve `core`; el replay los lee con el rol de `transfer-service` (solo `SELECT`). Es una lectura directa a la base de otro servicio, aceptada solo en la herramienta de simulación.
