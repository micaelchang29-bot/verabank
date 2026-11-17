# verabank-ml

Pipeline de ML de VeraBank (docs/diseno.md, ADR-05): Python entrena y exporta a ONNX; `risk-service` sirve.
Por ahora contiene el entorno, la prueba de humo de conversión a ONNX, la descarga de datos y el EDA.

## Uso

Requiere [uv](https://docs.astral.sh/uv/) (la CI usa 0.13.8). Desde la raíz del repositorio:

```bash
make ml-env    # uv sync --project ml --frozen
make ml-test   # pytest ml/tests -m "not data"
make data      # descarga PaySim a data/raw/paysim.csv (idempotente; hash en ml/data.sha256)
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
│   └── paysim.py             # esquema esperado de PaySim (check-paysim)
└── tests/
    ├── test_onnx_smoke.py    # paridad ONNX vs predict_proba, datos sintéticos, tolerancia 1e-5
    └── test_paysim_data.py   # marcador data: columnas, tipos y filas de PaySim
```

Contrato de los `.onnx`: entrada `features`, float32 `[None, n_features]`; la probabilidad de la clase
positiva sale de `predict_positive_proba` (CPU, un hilo).

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
