# ADR-13 · Decisiones de la fase 5 (BI, drift, auditoría verificable, reentrenamiento, carga y CI)

- Contexto: la fase 5 añade el esquema `analytics` (vista de §4, `drift_metrics`, `fraud_reasons`), el verificador de la
  cadena de auditoría (T-16), Metabase, el PSI con su demostración, las razones de fraude, el reentrenamiento con
  promoción y rollback, la carga T-17 y la CI completa. El diseño (§4, §5e–§5g, §6, §7) y el manual dejan sin fijar
  doce detalles; este ADR los registra (D1 a D12) con las concreciones que aparecieron al construir y los límites
  conocidos. Las cifras de §1.2 son objetivos, no resultados.

Cada decisión sigue el orden contexto, decisión, consecuencias.

## Decisiones

### D1 · Referencia y bins del PSI

- Contexto: §5f fija «10 bins por deciles de entrenamiento» y la fórmula, pero no cómo tratar bordes repetidos,
  variables de pocos valores, bins vacíos ni el score de referencia.
- Decisión: referencia = partición de entrenamiento (pasos 1–400), en `ml/artifacts/psi_reference.json`, solo
  agregados (bordes, proporciones, `top_features`, `model_version`, `model_sha256`), sin marcas de tiempo y
  determinista; su hash va en `manifest.json`. 10 bins por deciles con bordes únicos (`(borde[i-1], borde[i]]`); una
  feature con ≤ 10 valores distintos en entrenamiento tiene un bin por valor más «otro»; un solo bin ⇒ `degenerate`,
  PSI 0. Proporciones acotadas a ≥ 1e-4, sin renormalizar. Score de referencia: el del champion sobre entrenamiento,
  redondeado a 5 decimales como en `scoring_log`. `top_features`: 5 de mayor SHAP medio absoluto (TreeSHAP exacto
  sobre 200.000 filas de entrenamiento elegidas con la semilla) o `|coef·desv|` con la logística. Los valores se
  redondean a 9 decimales antes de binarizar.
- Consecuencias: el mismo código binariza la referencia y cada ventana. Cambiar de champion obliga a regenerar la
  referencia (`ml-export` solo conserva su entrada en el manifiesto si es del mismo `model_sha256`, y una prueba lo
  exige). El redondeo a 9 decimales evita que la diferencia de último bit entre Java y Python en `hour_sin`/`hour_cos`
  cambie de bin un valor que cae en un borde. Con `fraud-xgb/6`: `type_PAYMENT`, `dst_seen_before`, `type_CASH_OUT`,
  `type_TRANSFER`, `log_amount`; `is_new_beneficiary` es degenerada (constante en entrenamiento).

### D2 · Ventanas y alerta

- Contexto: §5f dice «último día simulado» y «dos ventanas seguidas», sin alineación, completitud ni mínimo de filas.
- Decisión: ventanas de 24 pasos alineadas a `sim_epoch`: la `k` cubre `[24k+1, 24k+24]` (la 24 es 577–600). Datos:
  filas reales de `risk.scoring_log`, es decir, sin `model_version = 'warmup'` **y sin las filas sintéticas de
  `make perf-scoring`** (`source_ref` con prefijo `perf-`, sin transferencia; docs/resultados/fase-4.md). Completa si
  hay filas con `occurred_at >= window_end`. Menos de 200 filas ⇒ `insufficient`, sin alerta. `alert = true` solo para
  `score` y las `top_features`, con PSI > 0,25 en la ventana y en la anterior contigua; las demás se registran sin
  alerta. Upsert por `(window_start, feature)` con el rol `ml_job`.
- Consecuencias: el job es idempotente y solo calcula ventanas cerradas. Efecto de la regla de completitud: la última
  ventana con datos nunca está completa; en una tanda que termina en un borde de ventana, su última ventana solo se
  calcula cuando llega la tanda siguiente. La exclusión de `perf-` la comparten los tres jobs (PSI, razones y
  reentrenamiento): sin ella, el máximo `occurred_at` de las corridas de k6 (paso ≈ 1.800) dejaba sin madurar todas las
  etiquetas del reentrenamiento.

### D3 · Línea base de la demo

- Contexto: el PSI compara contra entrenamiento (pasos 1–400) y el replay reproduce la partición de prueba (575–744):
  el EDA de la fase 3 vio un cambio de régimen cerca del paso 400.
- Decisión: tandas alineadas a ventanas: base `--first-step 577 --steps 48`, deriva `--first-step 625 --steps 48
  --amount-factor 3`. `make drift-demo` juzga por **la ventana en que cae** cada alerta, no por cuándo se calculó: base
  = ventanas 577–624, deriva = 625–672 (la ventana 601–624 de la base solo se completa con la tanda de deriva). Si la
  base alerta o la deriva no, la demo se detiene con código 1 y muestra las tablas de PSI por feature y ventana: no se
  cambian umbral, referencia, ventanas ni top-5 sin decisión explícita.
- Consecuencias: sobre la base de la fase 4, la ventana 577–600 ya da PSI del score 1,17 y de `dst_seen_before` 1,03,
  y la 601–624 da 0,66 y 0,71: con la regla de D2 la **base alerta**, y la demo, tal como está definida, debe parar en
  el STOP. Es el cambio de régimen previsto, no un error del job. Queda pendiente de decisión (ver «Límites
  conocidos»); la corrida sobre base limpia está en las instrucciones de cierre.

### D4 · `drift-demo` y `reset-data`

- Contexto: la demo necesita una base sin datos anteriores y no hay forma de borrarlos sin perder todo.
- Decisión: `make reset-data` ejecuta `docker compose down -v --remove-orphans` con todos los perfiles y detiene el
  transfer-service del host; `make drift-demo` lo invoca. Ambos exigen `CONFIRM=yes`; sin él no ejecutan nada (listan los
  volúmenes que borrarían y salen con 1).
- Consecuencias: borran PostgreSQL, Kafka, el estado del rail y la base de Metabase. La demo ocupa 96 pasos.

### D5 · Verificador de la cadena (T-16)

- Contexto: el trigger encadena el hash, pero nada lo recalcula.
- Decisión: `audit.verify_chain(p_from)`, nueva y de solo lectura, recalcula con la fórmula del trigger y devuelve
  `(checked, first_bad_id, problem)` con `GAP`, `PREV_MISMATCH` o `HASH_MISMATCH`. No toca tablas ni triggers de
  `audit`. Vive en las migraciones de `analytics`, que ejecuta el rol de arranque, para que su dueño no sea un rol de
  servicio; `EXECUTE` revocado a `PUBLIC` y sin conceder. `make verify-audit` sale con 1 si hay problema.
- Consecuencias: detecta alterar, borrar o reencadenar una fila intermedia (pruebas con superusuario y trigger
  desactivado dentro de transacciones revertidas). Límite declarado: sin ancla externa no detecta truncar el final ni
  reescribir la cadena entera desde una fila. 120.012 filas en 0,4 s.

### D6 · Razones de fraude (§5e)

- Contexto: ONNX no devuelve SHAP.
- Decisión: job por lotes (`make ml-reasons`, rol `ml_job`) para los casos `REVIEW`/`BLOCK` del modelo (el fallback no
  tiene fila), explicados con el alias `champion` de MLflow y solo si ese mismo modelo los decidió. Tres razones en
  `analytics.fraud_reasons`, las de mayor contribución absoluta (desempate por nombre), con signo: SHAP
  (`shap.TreeExplainer`) con XGBoost, coeficiente × valor estandarizado con la logística. Idempotente por
  `model_version`.
- Consecuencias: el valor base de la aditividad se toma del sesgo de `pred_contribs` de XGBoost, porque
  `TreeExplainer.expected_value` ignora `base_score` de XGBoost 3 hasta la primera llamada a `shap_values`.

### D7 · Reentrenamiento (HU-10)

- Contexto: el entrenamiento offline usa PaySim; el reentrenamiento debe usar lo que el sistema vio.
- Decisión: `risk.scoring_log` ⨝ `risk.fraud_labels` con `ml_job`; solo filas maduras (≥ 48 pasos antes del máximo
  `occurred_at`) y reales (D2). Split temporal interno: último 25 % de los pasos para validación, hueco de 24. Familia
  e hiperparámetros del champion, pesos de clase, sin remuestreo, umbrales solo en esa validación. Se niega bajo 100
  fraudes de entrenamiento o 30 de validación. Produce el `challenger` (run en MLflow con sus umbrales adjuntos,
  `ml/artifacts/challenger/`, `golden_challenger.jsonl` recalculado y manifiesto). Disparadores: alerta de drift vigente,
  caída de recall > 5 puntos, > 30 días simulados desde el corte de entrenamiento.
  **Concreción:** los parámetros van en `ml/retrain.yaml`, no en `ml/config.yaml`: el SHA-256 de `config.yaml` es parte
  de la procedencia del champion (`thresholds.json`, `manifest.json` y la prueba T-13 de procedencia), y editarlo
  rompería la exportación idéntica de D8.
- Consecuencias: el challenger puede ser otra versión del mismo modelo registrado (`fraud-xgb/7`); la prueba de
  artefactos ahora exige que sea distinto del champion por `(modelo, versión)`. Desajuste de historia heredado (4/D1):
  `scoring_log` tiene menos historia que PaySim completo y el challenger aprende esa distribución; el informe lo declara.

### D8 · Promoción y rollback

- Contexto: §5f: «en sombra un día, −2 % de costo sin empeorar el recall; mover el alias `champion`».
- Decisión: alias de MLflow (un alias vive en un solo modelo registrado). `promote` usa las filas con `shadow_score` y
  etiqueta madura del champion vigente; exige que el challenger reproduzca ese `shadow_score` (≥ 99,5 %: si no, la sombra
  era otro modelo), ≥ 24 pasos, costo (fórmula de `config.yaml`, cada modelo con sus umbrales) ≥ 2 % menor y recall de
  REVIEW o BLOCK no peor. Guarda `previous_champion` en una etiqueta, deja al anterior de challenger y ejecuta
  `ml-export` y `ml-psi-ref`. `rollback` hace lo inverso. `ml-export` lee los umbrales del run del alias `champion`
  (`ml-train` y `retrain` los adjuntan); el champion `fraud-xgb/6`, anterior a la fase 5, no los tiene y usa
  `ml/artifacts/thresholds.json` si es de esa versión.
- Consecuencias: con el champion actual, la exportación antes y después del cambio dejó `model.onnx`,
  `thresholds.json`, `golden.jsonl`, `manifest.json`, `challenger/model.onnx` y `golden_challenger.jsonl` idénticos byte
  a byte. Promover de verdad exige reconstruir risk-scoring con el challenger, ≥ 24 pasos de tráfico en sombra y 48 más
  para que maduren sus etiquetas.

### D9 · Metabase

- Contexto: la edición open source no exporta dashboards (manual 0.2).
- Decisión: `metabase/metabase:v0.64.6.3` (línea LTS 64, verificada el 2026-11-17), nunca `latest`; perfil `bi`,
  `-Xmx1g`, `mem_limit` 1536m. Base de aplicación `metabase` en la misma instancia con el rol `metabase_app`; datos con
  `verabank_bi` (el `bi_ro` del manual). Dashboard `VeraBank — Riesgo` por API (`bi/setup_metabase.py`), idempotente.
  El aviso de drift es una tarjeta del dashboard (el correo exigiría SMTP).
- Consecuencias: la conexión filtra `analytics` y `risk`; `verabank_bi` no lee `core`. Una segunda ejecución del
  script solo actualiza.

### D10 · Carga T-17

- Contexto: §1.2 fija 50 TPS 30 min con pico de 100 TPS 5 min y 10 % de rechazos del rail.
- Decisión: canal `loadtest` con API key propia en `.env`; 70 % `EXTERNAL`, 30 % `INTERNAL`; montos de 10 a 500 (bajo
  el umbral AML y el límite del fallback); 2 % de reintentos con la misma clave y cuerpo; rail con `REJECT=0.1`;
  después `settle --approve-reviews`, `make reconcile-full` y `make verify-audit`. Base limpia y antes de sembrar el BI.
  `reconcile-full` (c) compara las filas del canal con los `201` de k6 **más los `422` que crean fila**
  (`transfer-declined` e `insufficient-funds`: toda solicitud válida deja fila, ADR-03).
- Consecuencias: un `BLOCK` del modelo es un `422` esperado, no un error.

### D11 · CI

- Contexto: el runner estándar tiene 4 CPU y 16 GB; PaySim no se puede descargar en la CI (licencia CC BY-SA y
  fragilidad).
- Decisión: muestra **sintética** de 50.000 filas con el esquema de PaySim generada en el job; verificación de firmas
  en `.githooks/check-signatures.sh` (excluido del `grep` del manual, que si no la detectaría a sí misma); kind solo por
  `workflow_dispatch`; carga, T-17 y caos fuera de la CI.
- Consecuencias: la CI prueba el pipeline, no las métricas del modelo.

### D12 · kind (ampliación)

- Contexto: ampliación fuera de la línea de corte (manual 0.1).
- Decisión: módulo opcional con Kustomize, el rail simulado, un Job de Flyway y otro de tópicos; imágenes con
  `kind load`; secretos desde `.env`; `kind-deploy` aborta si Compose corre.
- Consecuencias: **no se construyó en esta fase**: `kind` no está instalado en el equipo y 5.A–5.H no quedaron cerrados
  con sus corridas sobre base limpia (ver «Límites conocidos»).

## Límites conocidos

- **PSI de referencia de entrenamiento frente a un replay de otro régimen (D3).** La base de la demo alerta por sí
  sola; decidir entre cambiar la referencia (p. ej., validación o el inicio del replay), el umbral o la línea base es
  una decisión de diseño pendiente.
- **Verificador sin ancla externa (D5).**
- **Desajuste de historia heredado en el reentrenamiento (D7, 4/D1).**
- **Muestra sintética en la CI (D11).**
- **Filas sintéticas de k6 en `scoring_log` (D2).** Se excluyen por el prefijo `perf-` de `source_ref`; un generador
  nuevo que no lo use contaminaría PSI, razones y reentrenamiento.
- **Transferencias con reloj real en la línea de tiempo simulada.** `make smoke`, `make demo-idempotency` y la carga T-17
  envían `occurred_at` = ahora (2026), muy después de los pasos de PaySim (enero de 2026). En la misma base desplazan el
  máximo `occurred_at`: el PSI calcula cientos de ventanas vacías (`insufficient`), todas las etiquetas cuentan como
  maduras y el disparador de 30 días se cumple siempre. Por eso la demo, el BI y la carga corren sobre bases limpias y
  separadas (D3, D10).
