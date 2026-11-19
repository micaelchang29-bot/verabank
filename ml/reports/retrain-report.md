# Reentrenamiento: champion frente a challenger

Generado por `make retrain` (motivo: `manual`). Ambos modelos se evalúan sobre **la misma** validación interna, cada uno
con sus umbrales: el champion con los de `thresholds.json` (fijados offline) y el challenger con los suyos, fijados en
esta validación (por eso su costo es optimista frente al del champion: la comparación que decide es la de `promote`,
en sombra).

## Datos (`risk.scoring_log` ⨝ `risk.fraud_labels`, rol `ml_job`)

| Concepto | Valor |
|---|---|
| filas con etiqueta | 124,001 |
| excluidas por `warmup` | 0 |
| excluidas por etiqueta inmadura (< 48 pasos del máximo, paso 720) | 51,075 |
| entrenamiento: pasos | 577–624 (55,359 filas, 510 fraudes) |
| hueco | 625–648 |
| validación: pasos | 649–672 (11,084 filas, 247 fraudes) |

## Métricas en la validación interna

| Métrica | champion `fraud-xgb/6` | challenger `fraud-xgb/8` |
|---|---|---|
| PR-AUC | 0.5221 | 0.6300 |
| recall a FPR 0,5 % | 0.5182 | 0.4899 |
| recall a FPR 0,1 % | 0.3846 | 0.4170 |
| recall de REVIEW o BLOCK | 0.4737 | 0.4332 |
| tasa de revisión | 0.0412 | 0.0045 |
| FPR de bloqueo | 0.0000 | 0.0004 |
| costo | 85,117,208.62 | 77,029,647.89 |
| costo sin modelo | 377,872,026.36 | 377,872,026.36 |

## Límites declarados

- **Historia acortada (heredado de 4/D1).** El replay carga solo 24 pasos de calentamiento, así que `dst_seen_before`,
  `dst_age_hours` e `is_new_beneficiary` de `scoring_log` ven menos historia que en el entrenamiento offline: el
  challenger aprende esa distribución, no la de PaySim completo.
- La partición de prueba de PaySim es la fuente del replay: este reentrenamiento entrena con ella (lo que el sistema
  vio en producción simulada); el champion offline nunca la usó.
- Volumen pequeño frente al offline; los costos unitarios son los supuestos de `config.yaml`.
