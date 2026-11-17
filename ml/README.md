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
