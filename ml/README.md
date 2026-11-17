# verabank-ml

Pipeline de ML de VeraBank (docs/diseno.md, ADR-05): Python entrena y exporta a ONNX; `risk-service` sirve.
Por ahora contiene solo el entorno y la prueba de humo de conversión a ONNX.

## Uso

Requiere [uv](https://docs.astral.sh/uv/) (la CI usa 0.13.8). Desde la raíz del repositorio:

```bash
make ml-env    # uv sync --project ml --frozen
make ml-test   # pytest ml/tests -m "not data"
```

Desde `ml/`: `uv run python -m verabank_ml <subcomando>` (el despachador está en `cli.py`; aún no hay
subcomandos) y `uv run ruff check .`.

La fuente de verdad de las versiones es `uv.lock`; `requirements.txt` es solo una referencia
(`uv export --frozen --no-hashes`). Tras cambiar dependencias: `uv lock` y regenerarlo.

Marcadores de pytest (con `--strict-markers`): `data` (necesita PaySim en `data/raw/`) y `slow`.

## Estructura

```
ml/
├── pyproject.toml, uv.lock, .python-version, requirements.txt
├── src/verabank_ml/
│   ├── __main__.py, cli.py   # python -m verabank_ml <subcomando>
│   └── onnx_export.py        # sklearn Pipeline y XGBClassifier -> ONNX; evaluación con ONNX Runtime
└── tests/test_onnx_smoke.py  # paridad ONNX vs predict_proba, datos sintéticos, tolerancia 1e-5
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
