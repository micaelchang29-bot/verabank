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
- **Segunda referencia (`valid`).** `psi-reference --partition valid` y `psi --reference valid` construyen bordes,
  proporciones y score de referencia con la partición de validación (pasos 425–550) con las mismas reglas, en
  `psi_reference_valid.json` (hash propio en `manifest.json`) y con los mismos `top_features` que la de train. Por
  defecto sigue siendo `train` (§5f). Cada fila de `analytics.drift_metrics` lleva `reference` (migración V6, dentro de
  la clave; las filas previas pasan a `train`) y la regla de dos ventanas contiguas solo compara filas de la misma
  referencia. `promote`, `rollback` y `make ml-psi-ref` regeneran las dos.
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
- **Actualización (referencia `valid` y guarda del reloj).** `make drift-demo` calcula el PSI contra `valid` y exige
  con ella: base sin alertas y deriva con alertas; contra `train` imprime la misma tabla como hallazgo, sin criterio de
  aceptación. Umbral 0,25, ventanas de 24 pasos, regla de dos ventanas y top-5 **no se tocan**: subir el umbral para que
  la base no alerte sería ajustar el criterio a la demo. `bi-seed`, `drift-demo` y `retrain` abortan si hay
  transferencias con `occurred_at` fuera de `sim_epoch` a `sim_epoch` + 744 h (`--force`/`FORCE=1` la salta).
- **Hallazgo contra `train`.** La causa que se suponía (cambio de régimen cerca del paso 400) no basta para explicarlo:
  en la ventana 577–600 de la base de desarrollo, `dst_seen_before = 1` es el 4,4 % de las filas (1.915 de 43.825),
  frente al 45,9 % de la referencia de train y el 43,1 % de la de valid. El replay carga solo 24 pasos de calentamiento
  (4/D1, «desajuste de historia heredado»), así que las features de historia y el score ven mucha menos historia que
  en el entrenamiento offline. Esa base está además contaminada (pasos repetidos por siembras aditivas y filas con reloj
  real), de modo que no sirve como línea base. Sobre ella, `valid` da PSI del score 1,12 y de `dst_seen_before` 1,09 en
  577–600: **no resuelve la alerta**. Si en una base limpia ocurre lo mismo, la demo debe terminar en STOP y la decisión
  de fondo (calentamiento más largo en el replay o referencia construida con la misma historia truncada) es del
  usuario. La corrida limpia (`make drift-demo CONFIRM=yes`) no se ejecutó en esta sesión.
- Consecuencias: sobre la base de la fase 4, la ventana 577–600 ya da PSI del score 1,17 y de `dst_seen_before` 1,03,
  y la 601–624 da 0,66 y 0,71: con la regla de D2 la **base alerta**, y la demo, tal como está definida, debe parar en
  el STOP. Es el cambio de régimen previsto, no un error del job. Queda pendiente de decisión (ver «Límites
  conocidos»); la corrida sobre base limpia está en las instrucciones de cierre.

### D13 · Calentamiento dirigido de historia (reabre 4/D1)

- Contexto: el replay cargaba solo 24 pasos de calentamiento (4/D1), suficientes para las ventanas de 1 h y 24 h pero no
  para `dst_seen_before` y `dst_age_hours`, que dependen de la **primera** aparición del destino (D6 de la fase 3). En la
  ventana 577–600 de la base de desarrollo, `dst_seen_before = 1` era el 4,4 % de las filas (1.915 de 43.825), frente al
  45,9 % de la referencia de `train` y el 43,1 % de la de `valid`; el PSI del score contra ambas rondaba 1,1 y la causa
  era el recorte de historia, no un cambio de régimen.
- Decisión: además de los 24 pasos previos completos, por cada destino `PS-<nombre>` del lote que ya apareció en PaySim
  antes de `primer_paso - 24` (solo los tipos de `config.yaml`, sin `CASH_IN`; cuenta cualquier monto, como el
  entrenamiento) se carga UNA fila en `risk.scoring_log`, en el paso de su primera aparición: `model_version = 'warmup'`,
  `features = '{}'`, `score = 0`, `decision = 'APPROVE'`, mismo mapeo de `source_ref` y `dest_ref` (D10 de la fase 4),
  `transfer_id` = `uuid5` del destino con un espacio de nombres propio, `COPY` con el rol `sim_loader` y
  `ON CONFLICT DO NOTHING` si hay un reintento. En tandas encadenadas solo se cargan los destinos cuya fila más antigua
  en `scoring_log` es posterior a su primera aparición. Se informa antes de enviar (destinos del lote, con historia,
  ya cargados, filas a cargar). Activo por defecto en `replay`, `bi-seed` y `drift-demo`; `--no-history-warmup` lo apaga.
  `is_new_beneficiary` no se toca. Las filas `warmup` siguen excluidas de PSI, razones, reentrenamiento y KPIs
  (pruebas en `ml/tests/test_psi.py` y `simulator/tests/test_history.py`).
- Auditoría de paridad: `make feature-parity` (`python -m verabank_sim feature-parity --sample 5000`) compara las
  `features` de `risk.scoring_log` con `features_A.parquet` por `row_id`.
- Medido (base limpia tras `make reset-data` y `make up`; `replay --steps 48 --first-step 577`, 55.359 filas, 23.341
  filas de historia de 54.556 destinos): **5.000 de 5.000 filas con pareja y 0 diferencias en las 17 features**
  (tolerancia 1e-9), incluidas `dst_seen_before`, `dst_age_hours` e `is_new_beneficiary`. `dst_seen_before = 1` es el
  44,5 % de la ventana 577–600 (19.910 de 44.764), frente al 45,9 % (`train`) y el 43,1 % (`valid`). PSI de las top-5
  features ≤ 0,03 contra ambas referencias. PSI del score: 0,425 (577–600) y 0,371 (601–624) contra `train`; 0,018 y
  0,028 contra `valid`.
- `make drift-demo CONFIRM=yes` con el calentamiento dirigido (base limpia, referencia `valid`): la base no alertó (0
  filas en 577–624; PSI del score 0,018 y 0,028) y la deriva **tampoco** (código 2, STOP de D3). La ventana 625–648 sí
  supera el umbral (score 0,379 y `log_amount` 0,653 contra `valid`), pero la regla de D2 pide dos ventanas contiguas y
  la 649–672 es la última de la tanda: nunca está completa, así que no se calcula y no puede confirmar la alerta. Es el
  efecto de completitud ya descrito en D2, no un fallo del job; resolverlo (una tanda de deriva de 72 pasos o una cola
  corta que cierre la ventana) cambia la demo y queda a decisión del usuario. Contra `train` alerta el score en 601–624
  y 625–648 (0,371 y 0,922). Hallazgo sin explicar: fuera del top-5, `dst_age_hours`, `dst_in_*` y `dst_distinct_src_24h`
  dan PSI de 0,9 a 1,6 contra `train` y `hour_sin`/`hour_cos` de 0,2 a 2,2 contra ambas; no alertan por no ser top-5.
- Consecuencias: el desajuste de historia deja de explicar el PSI. Contra `train` solo queda el **score** por encima de
  0,25 en dos ventanas seguidas, con las features estables: es la diferencia real de distribución del score entre el
  régimen de entrenamiento y el del replay (cambio cerca del paso 400). La base alertaría contra `train` por el score y
  no contra `valid`. Umbral, ventanas, referencia por defecto y top-5 no se tocan; qué referencia vigila producción es
  decisión del usuario. Sigue sin cargarse la historia de más de 24 h para las ventanas: no hace falta, la primera
  aparición queda fuera de ellas.

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

- **PSI del score contra `train` (D3, D13).** Corregido el recorte de historia, las features dejan de alertar, pero el PSI
  del score contra `train` sigue en 0,37–0,42 en la base: es la diferencia de régimen entre entrenamiento y replay. Contra
  `valid` no alerta. Decidir qué referencia vigila el servicio es una decisión de diseño pendiente.
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
