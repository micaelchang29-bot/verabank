# Artefactos del modelo de fraude

Salida de `make ml-export`. Los consume `risk-service` (fase 4) y las pruebas de paridad (T-13).
Todo lo de esta carpeta está versionado y es determinista: dos ejecuciones seguidas dejan archivos idénticos.

| Archivo | Qué es |
|---|---|
| `model.onnx` | Modelo servido (alias `champion` de MLflow: XGBoost). Entrada `features`, float32 `[None, 17]` en el orden de `thresholds.json:feature_order`. Salidas `label` y `probabilities` `[None, 2]`: la probabilidad de la clase positiva (fraude) es la **columna 1** de `probabilities`. |
| `shadow.onnx` | Modelo en sombra (alias `challenger`: regresión logística). Mismo contrato de entrada y salida. |
| `thresholds.json` | `model_version`, `model_sha256`, `feature_order`, `t_review`, `t_block`, `rounding_decimals` (5), regla de decisión y las métricas de `valid` que respaldan los umbrales. |
| `golden.jsonl` | 1.000 casos para la paridad T-13 (ver abajo). |
| `manifest.json` | SHA-256 de cada artefacto, modelos exportados, contrato de entrada y salida del ONNX, y hashes de entrada (`features.yaml`, `config.yaml`, CSV). |

## Decisión

`score = round(p, 5)`; `BLOCK` si `score >= t_block`; si no, `REVIEW` si `score >= t_review`; si no, `APPROVE`.
El redondeo es a 5 decimales, a la mitad par sobre la representación decimal exacta del `double` (el `round` de
Python). Con los scores de `valid` coincide con `np.round` en todas las filas.

## `golden.jsonl`

Una línea por caso: `case_id`, `stratum`, `request`, `history`, `features`, `score`, `decision`.

- `request`: `occurred_at` (= `sim_epoch` + `step - 1` horas, UTC), `type`, `amount`, `source`, `dest`.
- `history`: todas las filas anteriores (`occurred_at` estrictamente menor) del mismo origen o del mismo destino,
  solo de los tipos permitidos, en orden cronológico. Es lo único que hace falta para recalcular las features
  (ventanas de 1 h y 24 h, destino visto, antigüedad del destino, beneficiario nuevo) según la sección «Features»
  de `ml/README.md`. Hasta `golden.max_history` (300) filas por caso.
- `features`: las 17 features del contrato, en el orden de `feature_order`. Las sumas pueden diferir en el último
  dígito según el orden de suma: se comparan con tolerancia relativa de 1e-9.
- `score`: probabilidad `float64` del modelo original (`predict_proba` sobre las features en `float32`).
  ONNX Runtime debe reproducirla con diferencia < 1e-5.
- `decision`: resultado de aplicar la regla anterior a `score`.
- `stratum`: `fraud`, `above_review` (legítimas con score ≥ `t_review`), `boundary` (a menos de 0,0005 de un
  umbral, a ambos lados, incluidos casos en que el redondeo a 5 decimales cambia el lado) o `random`
  (legítimas al azar estratificadas por tipo).

Las cuentas son alias locales (`a1`, `a2`, …) que conservan las igualdades dentro de cada caso. Ningún nombre de
cuenta de PaySim aparece en estos archivos.

Los casos salen de `valid` (pasos 425–550). La historia de un caso nunca incluye filas posteriores a su paso, de
modo que ningún dato de `test` interviene. Composición y sesgo de la muestra: `ml/reports/golden_selection.json`.

## Cómo se regenera

```bash
make ml-features   # si cambió el contrato de features
make ml-train      # champion, challenger y thresholds.json (no incluye model_sha256)
make ml-export     # model.onnx, shadow.onnx, golden.jsonl, model_sha256 y manifest.json
make ml-test       # paridad T-13 sobre estos archivos (corre en la CI, sin datos)
```

`make ml-train` reescribe `thresholds.json` sin `model_sha256`; hay que volver a ejecutar `make ml-export`.
El exportador fija `graph.name` del grafo ONNX (los conversores ponen un UUID aleatorio) para que el archivo sea
reproducible byte a byte.

## Datos y licencia

Los casos de `golden.jsonl` derivan de **PaySim** (Kaggle `ealaxi/paysim1`, «Synthetic Financial Datasets For
Fraud Detection», Edgar Lopez-Rojas), licencia [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
Atribución requerida por la ficha del dataset:

> E. A. Lopez-Rojas, A. Elmir y S. Axelsson. «PaySim: A financial mobile money simulator for fraud detection».
> The 28th European Modeling and Simulation Symposium (EMSS), Larnaca, Chipre, 2016.

Cambios: se conservan tipo, monto y sentido de las transferencias, y solo los pasos temporales se convierten en
`occurred_at`; los nombres de cuenta se sustituyen por alias. `golden.jsonl` es material adaptado y se comparte bajo
CC BY-SA 4.0. El régimen de licencia de los pesos del modelo entrenado con PaySim (`model.onnx`, `shadow.onnx`) no se
ha determinado aquí.
