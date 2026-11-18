# Reentrenamiento: champion frente a challenger

Generado por `make retrain` (motivo: `drift`). Ambos modelos se evalúan sobre **la misma** validación interna, cada uno
con sus umbrales: el champion con los de `thresholds.json` (fijados offline) y el challenger con los suyos, fijados en
esta validación (por eso su costo es optimista frente al del champion: la comparación que decide es la de `promote`,
en sombra).

## Datos (`risk.scoring_log` ⨝ `risk.fraud_labels`, rol `ml_job`)

| Concepto | Valor |
|---|---|
| filas con etiqueta | 122,738 |
| excluidas por `warmup` | 0 |
| excluidas por etiqueta inmadura (< 48 pasos del máximo, paso 7709) | 0 |
| entrenamiento: pasos | 575–660 (64,221 filas, 928 fraudes) |
| hueco | 661–684 |
| validación: pasos | 685–720 (41,891 filas, 415 fraudes) |

## Métricas en la validación interna

| Métrica | champion `fraud-xgb/6` | challenger `fraud-xgb/7` |
|---|---|---|
| PR-AUC | 0.4302 | 0.4317 |
| recall a FPR 0,5 % | 0.5229 | 0.3928 |
| recall a FPR 0,1 % | 0.3807 | 0.2819 |
| recall de REVIEW o BLOCK | 0.4747 | 0.3783 |
| tasa de revisión | 0.0109 | 0.0041 |
| FPR de bloqueo | 0.0007 | 0.0003 |
| costo | 166,253,494.75 | 233,470,588.84 |
| costo sin modelo | 750,016,100.35 | 750,016,100.35 |

## Límites declarados

- **Historia acortada (heredado de 4/D1).** El replay carga solo 24 pasos de calentamiento, así que `dst_seen_before`,
  `dst_age_hours` e `is_new_beneficiary` de `scoring_log` ven menos historia que en el entrenamiento offline: el
  challenger aprende esa distribución, no la de PaySim completo.
- La partición de prueba de PaySim es la fuente del replay: este reentrenamiento entrena con ella (lo que el sistema
  vio en producción simulada); el champion offline nunca la usó.
- Volumen pequeño frente al offline; los costos unitarios son los supuestos de `config.yaml`.
