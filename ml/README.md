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
│   └── paysim.py             # esquema esperado de PaySim (check-paysim)
└── tests/
    ├── test_onnx_smoke.py    # paridad ONNX vs predict_proba, datos sintéticos, tolerancia 1e-5
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

Trabajo derivado en este repositorio: agregados y gráficos de `reports/` (sin filas del dataset). Por la
cláusula SA, cualquier derivado de los datos que se redistribuya debe ir bajo CC BY-SA 4.0 con esta atribución.
