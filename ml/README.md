# verabank-ml

Pipeline de ML de VeraBank (docs/diseno.md, ADR-05): Python entrena y exporta a ONNX; `risk-service` sirve.
Por ahora contiene el entorno, la prueba de humo de conversión a ONNX, la descarga de datos y el EDA.

## Uso

Requiere [uv](https://docs.astral.sh/uv/) (la CI usa 0.13.8). Desde la raíz del repositorio:

```bash
make ml-env    # uv sync --project ml --frozen
make ml-test   # pytest ml/tests -m "not data"
make data      # descarga PaySim a data/raw/paysim.csv (idempotente; hash en ml/data.sha256)
make ml-features  # features A y B en data/features/*.parquet (requiere make data)
make ml-train     # modelos del conjunto A, umbrales y selección (solo train y valid)
make ml-eval-test  # evaluación única en test del campeón (repetir exige ARGS=--force-test)
make ml-export     # ONNX del champion y del challenger, golden.jsonl, thresholds.json y manifest.json
make ml-report     # ablación B (solo valid) y reports/model-report.md
make eda       # ejecuta notebooks/01_eda.ipynb y regenera reports/eda.md y reports/figures/
(cd ml && uv run pytest -m data)   # esquema de PaySim; necesita make data
```

Desde `ml/`: `uv run python -m verabank_ml <subcomando>` (el despachador está en `cli.py`; aún no hay
subcomandos) y `uv run ruff check .`.

La fuente de verdad de las versiones es `uv.lock`; `requirements.txt` es solo una referencia
(`uv export --frozen --no-hashes`). Tras cambiar dependencias: `uv lock` y regenerarlo.

Marcadores de pytest (con `--strict-markers`): `data` (necesita PaySim en `data/raw/`) y `slow`.

## Estructura

```
ml/
├── config.yaml               # cortes, costos, umbrales y semilla (solo valores)
├── data.sha256               # SHA-256 de data/raw/paysim.csv
├── notebooks/01_eda.ipynb, reports/eda.md, reports/figures/
├── pyproject.toml, uv.lock, .python-version, requirements.txt
├── src/verabank_ml/
│   ├── __main__.py, cli.py   # python -m verabank_ml <subcomando>
│   ├── onnx_export.py        # sklearn Pipeline y XGBClassifier -> ONNX; evaluación con ONNX Runtime
│   ├── features.py           # features A y B, punto-en-el-tiempo, vectorizadas (subcomando features)
│   ├── split.py              # train / valid / test / gap según config.yaml
│   ├── reference.py          # implementación lenta por filas, solo para pruebas
│   ├── modeling.py, train.py, selection.py, latency.py, tracking.py   # make ml-train
│   ├── metrics.py, bootstrap.py          # FPR, recall, costo, umbrales, bootstrap por bloques
│   ├── eval_test.py, report.py           # make ml-eval-test, make ml-report
│   ├── export.py, golden.py              # make ml-export: ONNX, golden.jsonl, manifest.json
│   └── paysim.py             # esquema esperado de PaySim (check-paysim)
└── tests/
    ├── test_onnx_smoke.py    # paridad ONNX vs predict_proba, datos sintéticos, tolerancia 1e-5
    ├── test_artifacts.py, test_golden.py             # T-13 lado Python sobre ml/artifacts/ (sin datos)
    ├── test_metrics.py, test_selection_and_eval.py   # métricas a mano, umbrales, selección, guarda
    ├── test_features.py, test_split.py   # T-14, referencia, ejemplos a mano, guarda (sin datos)
    ├── test_features_data.py  # marcador data: columnas del contrato, NaN, fraude por partición
    └── test_paysim_data.py   # marcador data: columnas, tipos y filas de PaySim
```

Contrato de los `.onnx`: entrada `features`, float32 `[None, n_features]`; la probabilidad de la clase
positiva sale de `predict_positive_proba` (CPU, un hilo).

## Features

Contrato: `contracts/features.yaml` (no se modifica aquí). Implementación: `verabank_ml/features.py`.
Este apartado es la definición operativa que debe reproducir la implementación en Java.

**Regla temporal.** Sea `s` el `step` de la solicitud (1 paso = 1 hora; `occurred_at = sim_epoch + (s-1) h`).
La fila solo ve filas con `step < s`. Las filas del mismo paso no se ven entre sí. Ventana de 1 h = paso
`s-1`; ventana de 24 h = pasos `s-24` a `s-1`. Equivale a `[t - w, t)` del contrato si el replay da a las
filas de un mismo paso el mismo `occurred_at`, alineado a la hora. La historia es todo el dataset anterior,
sin importar la partición, y cuenta intentos (ADR-07), no solo transferencias completadas. Ninguna feature
usa etiquetas. Solo cuentan como historia las filas de los tipos de `config.types`: CASH_IN se descarta
antes de calcular (1.399.284 filas).

| Columna | Fórmula | Relleno |
|---|---|---|
| `amount` | monto de la solicitud | – |
| `log_amount` | `ln(1 + amount)` | – |
| `type_TRANSFER`, `type_CASH_OUT`, `type_PAYMENT`, `type_DEBIT` | one-hot de `type`, en ese orden | – |
| `hour_sin`, `hour_cos` | `sin` y `cos` de `2π · h / 24`, con `h = (s-1) mod 24` | – |
| `dst_in_count_24h` | nº de filas previas con el mismo destino en `[s-24, s-1]` | 0 |
| `dst_in_sum_24h` | suma de sus montos | 0.0 |
| `dst_distinct_src_24h` | nº de orígenes distintos entre esas filas | 0 |
| `dst_seen_before` | 1 si el destino aparece en alguna fila con `step < s` | 0 |
| `dst_age_hours` | `s - (primer step del destino)` si `dst_seen_before`; si no, 0 | 0.0 |
| `src_out_count_1h` | nº de filas previas del mismo origen en el paso `s-1` | 0 |
| `src_out_count_24h` | nº de filas previas del mismo origen en `[s-24, s-1]` | 0 |
| `src_out_sum_24h` | suma de sus montos | 0.0 |
| `is_new_beneficiary` | 1 si el par (origen, destino) no aparece en ninguna fila con `step < s` | 1 |

Conjunto B (ablación, no se sirve): A más `src_balance_before` (= `oldbalanceOrg`) y
`amount_to_balance = amount / src_balance_before`.

**Pendiente de contrato** (implementado como se indica; `features.yaml` no lo define y no se ha editado):

1. `amount_to_balance` con `src_balance_before = 0`: se define como `0.0` (el cociente no existe).
   Solo afecta al conjunto B.
2. `dst_age_hours` se mide como diferencia de pasos (horas enteras). El contrato dice «horas desde la primera
   aparición»; coinciden mientras `occurred_at` esté alineado a la hora.
3. Las filas de tipos fuera de `config.types` no son historia (no estarían en `scoring_log`).
4. El nombre y el orden de las columnas one-hot (`type_<categoría>`, orden de `categories`).
5. `dst_seen_before` y `is_new_beneficiary` toman como historia cualquier fila previa del destino o del par,
   de cualquier tipo permitido, incluidos los pagos a comercios (`M...`).

**Guardas.** `newbalanceOrig`, `oldbalanceDest`, `newbalanceDest` e `isFlaggedFraud` nunca se leen del CSV y,
si aparecen en la matriz, `LeakageError`; en el conjunto A también falla cualquier columna de saldo.

**Salida.** `data/features/features_{A,B}.parquet`: `row_id` (posición en el CSV), `step`, `type`, `is_fraud`,
`partition` (`train`, `valid`, `test` o `gap`), `src_id`, `dst_id` (solo análisis; nunca entran a la matriz) y
las features en el orden de `features.yaml`. `amount` es a la vez dato de la fila y primera feature (una sola
columna). Las filas `gap` o fuera de los cortes no se usan para entrenar ni evaluar, pero sí son historia.
Todas las columnas de features son `float64`.

Medidas (12 CPU, PaySim completo): conjunto A en 24 s y 4,6 GB de memoria máxima; B en 22 s y 4,6 GB.

## Contrato con el servicio de scoring

Es lo que implementa el servicio Java (`risk-service`) para que su score y sus features coincidan con los de
Python. Lo comprueban `ml/artifacts/golden.jsonl` (1.000 casos con historia) y
`contracts/fixtures/feature_scenarios.json` (14 escenarios pequeños; `make ml-fixtures`). Las
tolerancias son 1e-9 (relativa y absoluta) para las features y 1e-5 para el score.

**(a) Redondeo del score antes de comparar con un umbral.**

- Regla: `rounded = round(score, 5)` y luego `rounded >= t_block` → `BLOCK`; si no, `rounded >= t_review` →
  `REVIEW`; si no, `APPROVE`. Está en `ml/src/verabank_ml/golden.py:32` (`decide_score`, líneas 30-35) y se
  repite en `thresholds.json:decision_rule`. Los umbrales de `thresholds.json` ya tienen 5 decimales.
- Función: el `round(x, ndigits)` incorporado de Python 3, sobre un `float` (no `np.round`).
- Modo: mitad al par (`ROUND_HALF_EVEN`) sobre el valor decimal **exacto** del `double`, no sobre su
  representación impresa. Es equivalente a `Decimal(x).quantize(Decimal("0.00001"), ROUND_HALF_EVEN)`; se
  comprobó con 300.000 scores aleatorios en float32 sin ningún desacuerdo. En Java:
  `new BigDecimal(score).setScale(5, RoundingMode.HALF_EVEN).doubleValue()`. No sirven `Math.round(x * 1e5) / 1e5`
  (redondea la mitad hacia arriba y multiplica con error) ni `new BigDecimal(Double.toString(x))` (parte del
  texto más corto, no del valor exacto).
- Tipo numérico: la salida `probabilities` de ONNX Runtime es `float32`; se promueve a `double` (conversión
  exacta, sin pasar por texto) y se redondea ese `double`. El resultado se compara con los umbrales leídos
  como `double` de `thresholds.json`.
- El entrenamiento fija los umbrales con `np.round` (`ml/src/verabank_ml/metrics.py:21`). Con los scores de
  `valid` ambos coinciden en todas las filas (`ml/reports/golden_selection.json`:
  `rounding_numpy_vs_python_disagreements_in_valid` = 0), pero la regla de servicio es la de Python `round`.
- El `score` de `golden.jsonl` es el del modelo original (`float64`); ONNX Runtime lo reproduce con diferencia
  < 1e-5 (máxima observada 1,8e-7) y las 1.000 decisiones salen iguales con ambos. Los escenarios guardan
  `expected_score` sin redondear.

**(b) Tiempo.** `occurred_at = sim_epoch + (step - 1) h` con `sim_epoch = 2026-01-01T00:00:00Z`
(`config.yaml`). Es decir, `step = horas enteras desde sim_epoch + 1` y
`hour = (step - 1) % 24`, que es la hora UTC de `occurred_at`. Los eventos del entrenamiento, del golden y de
los escenarios están alineados a la hora exacta. `hour_sin = sin(2π · hour / 24)` y
`hour_cos = cos(2π · hour / 24)`, en `double`.

**(c) Ventanas.** Cerradas por la izquierda y estrictamente anteriores a la solicitud: una ventana de `w` horas
cubre `[t - w, t)`, con `t = occurred_at` de la solicitud. Con tiempos alineados a la hora: la ventana de 1 h
es el paso `s-1`; la de 24 h son los pasos `s-24` a `s-1`; un evento en `s-25` no cuenta. Las filas con
`occurred_at >= t` (incluidas las del mismo paso) nunca cuentan, así que dos solicitudes de la misma hora no se
ven entre sí. «Todo el historial» es toda fila previa (`occurred_at < t`), sin límite de ventana. La historia son
los intentos de los cuatro tipos del modelo (incluidos los rechazados y los pagos a comercios); CASH_IN no es
historia. Ninguna feature usa etiquetas.

**(d) Relleno de una feature sin historia.**

| Feature | Sin historia vale |
|---|---|
| `dst_in_count_24h`, `dst_in_sum_24h`, `dst_distinct_src_24h` | 0 |
| `dst_seen_before` | 0 |
| `dst_age_hours` | 0 (destino no visto) |
| `src_out_count_1h`, `src_out_count_24h`, `src_out_sum_24h` | 0 |
| `is_new_beneficiary` | 1 |

El resto (`amount`, `log_amount`, `type_*`, `hour_*`) sale solo de la solicitud. `dst_age_hours` es la diferencia
de pasos con la primera aparición del destino (horas enteras). Booleanos como `0.0` / `1.0`.

**(e) Orden y nombres de las columnas.** Entrada `features`, `float32` `[None, 17]`; las features se calculan
en `double` y se convierten a `float32` justo antes de llamar al modelo. Salida: `probabilities`, columna 1.

| # | Columna | # | Columna |
|---|---|---|---|
| 0 | `amount` | 9 | `dst_in_sum_24h` |
| 1 | `log_amount` | 10 | `dst_distinct_src_24h` |
| 2 | `type_TRANSFER` | 11 | `dst_seen_before` |
| 3 | `type_CASH_OUT` | 12 | `dst_age_hours` |
| 4 | `type_PAYMENT` | 13 | `src_out_count_1h` |
| 5 | `type_DEBIT` | 14 | `src_out_count_24h` |
| 6 | `hour_sin` | 15 | `src_out_sum_24h` |
| 7 | `hour_cos` | 16 | `is_new_beneficiary` |
| 8 | `dst_in_count_24h` | | |

`amount` es la cadena decimal de la solicitud convertida a `double`; `log_amount = ln(1 + amount)` (`log1p`).
Las columnas `type_*` son 1.0 en la del tipo de la solicitud y 0.0 en las demás. La fuente de verdad del orden
es `thresholds.json:feature_order` (= `contracts/features.yaml`).

**Challenger.** `ml/artifacts/challenger/model.onnx` usa las mismas entradas, salidas y `feature_order`.
No tiene umbrales ni decisión: el servicio solo guarda su score en sombra (`golden_challenger.jsonl` da su
score para los mismos `case_id` del golden).

## Modelos, umbrales y evaluación

Método (no negociable): hiperparámetros, umbrales y selección usan solo `valid`; `test` se evalúa una vez, con
todo congelado. `ml-train` ni siquiera puede leer `test` (`load_features` lanza `PermissionError`). Sin SMOTE:
pesos de clase. El escalador va dentro del `Pipeline`, ajustado solo con `train`. Semilla en `config.yaml`.

- **Modelos (conjunto A).** Regresión logística (`class_weight="balanced"`, rejilla de `C`) y XGBoost
  (`hist`, `aucpr`, parada temprana contra `valid`, `max_depth` 4–6, ≤ 400 árboles, rejilla de
  `scale_pos_weight`). Las rejillas están en `config.yaml`. Cada combinación es una ejecución hija de MLflow
  (experimento `verabank-fraud`, SQLite en `ml/mlflow.db`, no versionado). La mejor de cada familia se registra
  como `fraud-logreg` y `fraud-xgb`. El XGBoost final se reentrena con `best_iteration + 1` árboles y sin
  parada temprana, para que el modelo registrado y el ONNX contengan exactamente los mismos árboles.
  Se registran parámetros, métricas, `config.yaml`, hash de `features.yaml`, hash del CSV y commit.
- **Scores y decisión.** `score = round(p, 5)`; `BLOCK` si `score >= t_block`; si no, `REVIEW` si
  `score >= t_review`; si no, `APPROVE`. Las métricas de decisión usan scores redondeados; el PR-AUC
  (`average_precision`) usa probabilidades sin redondear.
- **`t_block`**: el menor score observado en `valid` con FPR de bloqueo ≤ `block_fpr_max`.
  **`t_review`**: el que minimiza `C` con tasa de revisión `n(REVIEW)/N ≤ review_rate_max` y
  `t_review ≤ t_block`; a igual costo gana el mayor umbral. Los candidatos son los scores observados, así que
  los empates por el redondeo no se pueden separar.
- **Costo** `C = Σ monto(fraude APPROVE) + review·n(REVIEW) + block_false_positive·n(legítimas BLOCK)`.
  Un fraude en `REVIEW` o `BLOCK` no suma monto. La sensibilidad multiplica ambos costos unitarios por 0,5 / 1 / 1,5
  con los umbrales fijos. Como los montos de los fraudes dominan, `C` casi no cambia con los costos unitarios.
- **Recall a FPR fija** (0,5 % y 0,1 %): el umbral se fija en `valid` (menor score con FPR ≤ objetivo) y se informa
  el FPR real; en `test` se usan esos mismos umbrales y se informa el FPR real de `test`.
- **Intervalos**: bootstrap de bloques de pasos (24 y, como sensibilidad, 6), 1.000 remuestreos, percentiles al
  95 %, semilla fija. Con menos de `bootstrap.min_blocks` (10) bloques el intervalo no es concluyente:
  `valid` tiene 6 bloques de 24 pasos y `test` 8 (advertencia, no parada).
- **Selección** (`selection.py`, `reports/selection.json`; regla D3): el boosting se sirve solo si mejora el PR-AUC
  en ≥ 0,02, con intervalos que no se solapan, baja el costo y su p95 en ONNX Runtime (un hilo, una fila por
  llamada, 10.000 llamadas) es < 10 ms. Los intervalos se evalúan con bloques de 24 pasos y se contrastan con
  los de 6: si el resultado difiere entre tamaños, cuenta como no cumplido. Tener menos de 10 bloques no
  detiene la selección: queda como advertencia en `selection.json` y en `model-report.md`.
  `make ml-train ARGS="--winner xgb|logreg"` es un override manual y queda registrado como tal.
- **`artifacts/thresholds.json`** (solo con ganador): `model_version`, `feature_set`, `feature_order`, `t_review`,
  `t_block`, `rounding_decimals`, regla de decisión y métricas de `valid`. Alias de MLflow: `champion` y `challenger`.
- **`ml-eval-test`**: una sola vez; si existe `reports/test_evaluation.json` o una ejecución de test en MLflow,
  se niega, salvo con `--force-test` (etiqueta `test_reevaluated=true`). Añade las métricas sobre destinos no
  vistos en `train`, FPR por tipo y por tramo de monto (décadas fijas) y la comparación con aprobarlo todo.
- **El EDA y `test`.** `notebooks/01_eda.ipynb` mide en `test`, de forma solo descriptiva, el solapamiento de
  destinos con `train` y los fraudes por partición (con los que se comprueba el mínimo de 500 por partición de
  §5c). No entrena, no elige hiperparámetros, umbrales ni modelo, y ninguna de esas cifras alimenta una decisión;
  la regla de «`test` una vez» se refiere a la evaluación del modelo, que solo hace `ml-eval-test`.

## Versiones fijadas

| Componente | Versión | Origen |
|---|---|---|
| Python | 3.12 | `.python-version`, `requires-python` |
| xgboost | 3.4.1 | fijada en `pyproject.toml` |
| onnxmltools | 1.16.0 | fijada en `pyproject.toml` |
| onnxruntime | 1.32.0 | fijada en `pyproject.toml` |
| skl2onnx | 1.20.0 | resuelta en `uv.lock` |
| onnx | 1.23.2 | resuelta en `uv.lock` |
| scikit-learn | 1.9.1 | resuelta en `uv.lock` |
| numpy | 2.5.5 | resuelta en `uv.lock` |

La prueba de humo pasó con XGBoost 3.4.1 + onnxmltools 1.16.0 (sin necesidad de retroceder a 3.1.x).

## Datos y licencias

Los datos no se versionan (`data/` está en `.gitignore`). `make data` descarga PaySim sin cuenta.

**PaySim** (Kaggle `ealaxi/paysim1`, «Synthetic Financial Datasets For Fraud Detection», de Edgar
Lopez-Rojas), licencia [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). La ficha pide citar:

> E. A. Lopez-Rojas, A. Elmir y S. Axelsson. «PaySim: A financial mobile money simulator for fraud
> detection». The 28th European Modeling and Simulation Symposium (EMSS), Larnaca, Chipre, 2016.

Trabajo derivado en este repositorio: agregados y gráficos de `reports/` (sin filas del dataset) y los artefactos
versionados de `artifacts/` (`golden.jsonl` contiene montos, tipos y tiempos de 1.000 transacciones de PaySim con las cuentas
sustituidas por alias; véase `artifacts/README.md`). Por la
cláusula SA, cualquier derivado de los datos que se redistribuya debe ir bajo CC BY-SA 4.0 con esta atribución.
Los pesos entrenados con PaySim (`artifacts/model.onnx` y `artifacts/challenger/model.onnx`), `golden.jsonl` y `golden_challenger.jsonl` se tratan como
material adaptado de PaySim y se distribuyen bajo CC BY-SA 4.0, con la atribución de la ficha del dataset; el código
del repositorio mantiene su propia licencia.

## Monitoreo de drift (fase 5, §5f, D1 y D2)

```bash
make ml-psi-ref                  # psi_reference.json (train) y psi_reference_valid.json (valid); hashes en manifest.json
make psi [ARGS=--reference valid --recompute]   # ventanas completas pendientes → analytics.drift_metrics (rol ml_job)
make drift-demo CONFIRM=yes      # DESTRUCTIVO: base 577–624, deriva 625–696 (72 pasos) con montos × 3, referencia valid
```

- **Referencia** (`monitor/reference.py`): solo la partición `train` (pasos 1–400) de `features_A.parquet`; solo
  agregados, sin marcas de tiempo y determinista (dos ejecuciones dan el mismo archivo byte a byte). Por feature, bins
  de `monitor/binning.py`; además el score del champion sobre train (redondeado a 5 decimales, como `scoring_log`),
  `top_features` (5 de mayor SHAP medio absoluto con TreeSHAP exacto de XGBoost sobre una muestra determinista de
  200.000 filas de train; `|coef·desv|` si sirviera la logística), `model_version` y `model_sha256`. Hay que
  regenerarla al cambiar el champion: `ml-export` solo conserva su entrada en el manifiesto si es del mismo
  `model_sha256`, y `tests/test_psi.py` lo exige.
- **Segunda referencia, `valid`** (`psi-reference --partition valid`, `psi --reference valid`): mismos bins y reglas,
  pero con la partición de validación (pasos 425–550, ya en el régimen posterior al cambio cercano al paso 400) y su
  propio archivo, `psi_reference_valid.json`, con su hash en `manifest.json`. Hereda los `top_features` de la de train.
  **Solo `valid` genera alertas** (desviación consciente de §5f, ADR-13 D14: los umbrales de decisión se fijaron en
  validación y contra `train` el score diverge sin deriva): es el valor por defecto de `psi`, de `drift-demo`, de
  `retrain --check` y del aviso de BI. `train` se calcula y se muestra (`psi --reference train`) como medida informativa
  del cambio de régimen, siempre con `alert = false`. `make ml-psi-ref`, `promote` y `rollback` regeneran las dos. Cada
  fila de `analytics.drift_metrics` lleva su `reference` (parte de la clave) y la alerta de dos ventanas contiguas solo
  compara filas de la misma referencia. Umbral 0,25, ventanas y top-5 no cambian.
- **Bins**: 10 por deciles con bordes únicos (`(borde[i-1], borde[i]]`); ≤ 10 valores distintos: uno por valor más
  «otro»; un solo bin ⇒ `degenerate`, PSI 0. Proporciones acotadas a ≥ 1e-4, sin renormalizar;
  PSI = Σ (a − e)·ln(a/e). Los valores se redondean a 9 decimales antes de binarizar (Java y Python difieren en el
  último bit de `hour_sin`/`hour_cos`). El mismo código sirve para la referencia y para cada ventana.
- **Ventanas** (`monitor/psi.py`): la `k` cubre los pasos `[24k+1, 24k+24]` (la 24 es 577–600). Filas de
  `risk.scoring_log` sin `warmup`. Completa si hay filas con `occurred_at >= window_end` (la última ventana con datos
  nunca lo está). Menos de 200 filas ⇒ `insufficient`. `alert` solo con `valid`, para `score` y las `top_features`, con PSI > 0,25
  en la ventana y en la anterior contigua (de la misma referencia). Upsert por `(window_start, feature, reference)`;
  `--fail-on-alert` sale con 2.
- **Guarda del reloj** (`clock_guard.py`, `make` → `scripts/ml-job.sh clock-guard`): `bi-seed`, `drift-demo` y `retrain`
  (también `retrain-check`) abortan si `risk.scoring_log` tiene transferencias con `occurred_at` fuera de `sim_epoch` a
  `sim_epoch` + 744 h (sin contar las sintéticas `perf-` de k6). **`make smoke`, `make demo-idempotency` y
  `make load-transfers` envían `occurred_at` de hoy y requieren su propia base** (`make reset-data CONFIRM=yes`
  antes de la demo, de la siembra de BI o del reentrenamiento). `FORCE=1` (`--force`) la salta.
- Con la referencia actual (champion `fraud-xgb/6`): `top_features` = `type_PAYMENT`, `dst_seen_before`,
  `type_CASH_OUT`, `type_TRANSFER`, `log_amount`; `is_new_beneficiary` es degenerada (constante en train).

## Razones de fraude (fase 5, §5e, D6)

`make ml-reasons [ARGS=--limit N]` (`verabank_ml/reasons.py`, rol `ml_job`) escribe tres filas por caso en
`analytics.fraud_reasons`. Casos: `decision` `REVIEW` o `BLOCK` de `risk.scoring_log`, sin `warmup` y decididos por el
mismo modelo que el alias `champion` (los de otra versión se cuentan como omitidos; el fallback no deja fila). Con
XGBoost, SHAP con `shap.TreeExplainer` en log-odds (la suma más el valor base reproduce el margen; el valor base sale
del sesgo de `pred_contribs`, porque `TreeExplainer.expected_value` ignora `base_score` de XGBoost 3 hasta la primera
llamada a `shap_values`); con la logística, coeficiente × valor estandarizado. Se guardan las tres de mayor
contribución absoluta (desempate por nombre) con su signo. `explain(model, X)` es pura y común a los dos modelos.
Idempotente: no recalcula los casos que ya tienen razones de la misma `model_version`.

## Reentrenamiento, promoción y rollback (fase 5, HU-10, D7 y D8)

```bash
make retrain-check               # disparadores: drift vigente, caída de recall > 5 puntos, > 30 días simulados
make retrain [REASON=drift]      # challenger desde scoring_log ⨝ fraud_labels maduras; ml/reports/retrain-report.md
make promote                     # mueve champion si en sombra baja el costo ≥ 2 % sin empeorar el recall
make rollback                    # vuelve al previous_champion
```

- Parámetros en `ml/retrain.yaml` (supuestos), **no** en `config.yaml`: su SHA-256 es parte de la procedencia del
  champion (`thresholds.json`, `manifest.json`, prueba de T-13) y cambiarlo rompería la exportación idéntica (D8).
- **Conjunto**: rol `ml_job`; sin `warmup`; solo etiquetas maduras (≥ 48 pasos antes del máximo `occurred_at`);
  features del JSONB en el orden del contrato; último 25 % de los pasos para validación con 24 de hueco; la familia y
  los hiperparámetros del champion (XGBoost con parada temprana y reajuste, como `ml-train`); umbrales solo en esa
  validación. Se niega por debajo de 100 fraudes de entrenamiento o 30 de validación.
- **Salida**: run en MLflow con su `thresholds.json` adjunto, alias `challenger` (en un solo modelo registrado),
  `ml/artifacts/challenger/{model.onnx,thresholds.json}`, `golden_challenger.jsonl` recalculado sobre los casos de
  `golden.jsonl` (paridad ONNX < 1e-5) y el manifiesto actualizado: risk-scoring lo carga en sombra al reconstruirse.
- **Promoción**: filas con `shadow_score` y etiqueta madura del champion vigente; exige que el challenger reproduzca el
  `shadow_score` registrado (≥ 99,5 %: si no, la sombra era de otro modelo), ≥ 24 pasos, costo (fórmula de
  `config.yaml`, cada modelo con sus umbrales) ≥ 2 % menor y recall de REVIEW o BLOCK no peor. Guarda
  `previous_champion` en una etiqueta de los modelos registrados, deja al anterior como challenger, ejecuta
  `ml-export` y `ml-psi-ref`, y avisa de reconstruir risk-scoring. `rollback` hace lo inverso.
- **`ml-export`** lee los umbrales del run del alias `champion` (`ml-train` y `retrain` los adjuntan). El champion
  `fraud-xgb/6` es anterior a la fase 5 y su run no los tiene: vale `ml/artifacts/thresholds.json` si es de esa versión.
  Con ese champion, la exportación antes y después del cambio dio `model.onnx`, `thresholds.json`, `golden.jsonl`,
  `manifest.json`, `challenger/model.onnx` y `golden_challenger.jsonl` idénticos byte a byte.
