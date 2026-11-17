# Informe del modelo de fraude

Generado por `make ml-report`. Hiperparámetros, umbrales y selección usan solo `valid`; `test` se evalúa una vez con todo congelado.

## Modelo servido
**xgb** (`fraud-xgb/5`), conjunto A. Motivo: el boosting cumple los cuatro criterios.

> **Advertencia:** valid tiene 6 bloques de 24 pasos (menos de 10): los intervalos por bootstrap de bloques son poco fiables.

| Criterio | Regla | Valor | Resultado |
|---|---|---|---|
| pr_auc_gain | PR-AUC(xgb) - PR-AUC(logreg) >= 0.02 | 0.2031 | PASS |
| pr_auc_intervals_do_not_overlap | low(xgb) > high(logreg) con bloques de 24 y de 6 pasos; si difieren, cuenta como no cumplido | {"block_steps_24": {"n_blocks_valid": 6, "logreg_pr_auc_interval": [0.24878556714140873, 0.47883977767545627], "xgb_pr_auc_interval": [0.48024692312267847, 0.6441889785785091], "no_overlap": true}, "block_steps_6": {"n_blocks_valid": 21, "logreg_pr_auc_interval": [0.25358485261661223, 0.47826266278809515], "xgb_pr_auc_interval": [0.48170484874820635, 0.6467254600838906], "no_overlap": true}} | PASS |
| cost_lower | costo(xgb) < costo(logreg) en valid, cada uno con sus umbrales | {"logreg": 533098846.56, "xgb": 530831837.98} | PASS |
| inference_p95 | p95 de xgb en ONNX Runtime < 10 ms | 0.004579 | PASS |

Intervalos de PR-AUC en `valid` con bloques de 24 pasos (6 bloques; el mínimo fiable es 10) y de 6 pasos (21 bloques):

| Modelo | Bloques de 24 pasos | Bloques de 6 pasos |
|---|---|---|
| logreg | [0.2488, 0.4788] | [0.2536, 0.4783] |
| xgb | [0.4802, 0.6442] | [0.4817, 0.6467] |

Latencia de inferencia (ONNX Runtime, un hilo, una fila por llamada, 10,000 llamadas): logreg p95 0.0049 ms; xgb p95 0.0046 ms.

## Métricas en valid (pasos 425–550)

### logreg (model=logreg, C=10.0)

| Métrica | Valor | Intervalo 95 % (bloques de 24) |
|---|---|---|
| PR-AUC | 0.3595 | [0.2488, 0.4788] |
| Recall a FPR 0.5% (FPR real 0.499%) | 0.4000 | [0.3534, 0.4383] |
| Recall ponderado por monto a FPR 0.5% | 0.7566 | [0.6889, 0.7966] |
| Recall a FPR 0.1% (FPR real 0.099%) | 0.1789 | [0.1399, 0.2134] |
| Recall ponderado por monto a FPR 0.1% | 0.6213 | [0.5314, 0.6702] |
| Costo C | 533,098,847 | [359358291, 653825821] |
| Costo sin modelo (todo aprobado) | 1,903,418,013 | – |
| Tasa de revisión / de bloqueo | 0.499% / 0.176% | – |
| FPR de bloqueo | 0.0493% | – |
| Recall de bloqueo / de revisión-o-bloqueo | 0.1305 / 0.3547 | – |

Sensibilidad del costo (umbrales fijos; costos unitarios ×0,5 / ×1 / ×1,5):

| review × / block_fp × | Costo |
|---|---|
| 0.5 / 0.5 | 533,097,386 |
| 0.5 / 1 | 533,097,866 |
| 0.5 / 1.5 | 533,098,346 |
| 1 / 0.5 | 533,098,367 |
| 1 / 1 | 533,098,847 |
| 1 / 1.5 | 533,099,327 |
| 1.5 / 0.5 | 533,099,348 |
| 1.5 / 1 | 533,099,828 |
| 1.5 / 1.5 | 533,100,308 |

### xgb (model=xgb, max_depth=6, scale_pos_weight=300, best_iteration=20)

| Métrica | Valor | Intervalo 95 % (bloques de 24) |
|---|---|---|
| PR-AUC | 0.5626 | [0.4802, 0.6442] |
| Recall a FPR 0.5% (FPR real 0.463%) | 0.5320 | [0.4933, 0.5590] |
| Recall ponderado por monto a FPR 0.5% | 0.8615 | [0.8190, 0.8883] |
| Recall a FPR 0.1% (FPR real 0.096%) | 0.3891 | [0.3471, 0.4190] |
| Recall ponderado por monto a FPR 0.1% | 0.6582 | [0.6075, 0.6882] |
| Costo C | 530,831,838 | [319697025, 729112713] |
| Costo sin modelo (todo aprobado) | 1,903,418,013 | – |
| Tasa de revisión / de bloqueo | 0.497% / 0.339% | – |
| FPR de bloqueo | 0.0416% | – |
| Recall de bloqueo / de revisión-o-bloqueo | 0.3055 / 0.4781 | – |

Sensibilidad del costo (umbrales fijos; costos unitarios ×0,5 / ×1 / ×1,5):

| review × / block_fp × | Costo |
|---|---|
| 0.5 / 0.5 | 530,830,455 |
| 0.5 / 1 | 530,830,860 |
| 0.5 / 1.5 | 530,831,265 |
| 1 / 0.5 | 530,831,433 |
| 1 / 1 | 530,831,838 |
| 1 / 1.5 | 530,832,243 |
| 1.5 / 0.5 | 530,832,411 |
| 1.5 / 1 | 530,832,816 |
| 1.5 / 1.5 | 530,833,221 |

Los umbrales de FPR fija (0,5 % y 0,1 %) se fijan en valid; el FPR real es el de esa partición.

## Umbrales (fijados en valid)

| Modelo | t_block (FPR de bloqueo ≤ 0,05 %) | t_review (mín. C, revisión ≤ 0,5 %, ≤ t_block) |
|---|---|---|
| logreg | 0.98974 | 0.95301 |
| xgb | 0.90380 | 0.83142 |

Decisión: `score = round(p, 5)`; BLOCK si `score >= t_block`; si no, REVIEW si `score >= t_review`; si no, APPROVE.

## Métricas en test (pasos 575–744, evaluado una vez)

Modelo: `fraud-xgb/4`, umbrales congelados de valid.

| Métrica | Valor | Intervalo 95 % (bloques de 24) |
|---|---|---|
| PR-AUC | 0.6029 | [0.5577, 0.6869] |
| Recall a FPR 0.5% (FPR real 0.840%) | 0.5226 | [0.4990, 0.5467] |
| Recall ponderado por monto a FPR 0.5% | 0.8815 | [0.8534, 0.9000] |
| Recall a FPR 0.1% (FPR real 0.070%) | 0.3963 | [0.3811, 0.4147] |
| Recall ponderado por monto a FPR 0.1% | 0.7261 | [0.6778, 0.7651] |
| Costo C | 713,132,559 | [488354899, 898966067] |
| Costo sin modelo (todo aprobado) | 2,979,091,950 | – |
| Tasa de revisión / de bloqueo | 1.001% / 0.455% | – |
| FPR de bloqueo | 0.0154% | – |
| Recall de bloqueo / de revisión-o-bloqueo | 0.2954 / 0.4817 | – |

La partición test tiene 8 bloques de 24 pasos (menos de 10: los intervalos son poco fiables) y 29 de 6 pasos.

### Destinos no vistos en train (59.3% de las filas de test; 1,306 fraudes)

| Métrica | Valor | Intervalo |
|---|---|---|
| PR-AUC | 0.6891 | – |
| Recall a FPR 0.5% (FPR real 0.512%) | 0.5651 | – |
| Recall ponderado por monto a FPR 0.5% | 0.8754 | – |
| Recall a FPR 0.1% (FPR real 0.114%) | 0.4250 | – |
| Recall ponderado por monto a FPR 0.1% | 0.6693 | – |
| Costo C | 630,340,493 | – |
| Costo sin modelo (todo aprobado) | 2,169,305,205 | – |
| Tasa de revisión / de bloqueo | 0.709% / 0.537% | – |
| FPR de bloqueo | 0.0261% | – |
| Recall de bloqueo / de revisión-o-bloqueo | 0.2902 / 0.5069 | – |

### FPR por tipo

| Tipo | Legítimas | Fraudes | FPR bloqueo | FPR revisión o bloqueo | Recall rev.+bloq. |
|---|---|---|---|---|---|
| CASH_OUT | 51639 | 931 | 0.000% | 1.423% | 47.2% |
| DEBIT | 1328 | 0 | 0.000% | 0.000% | – |
| PAYMENT | 55537 | 0 | 0.000% | 0.000% | – |
| TRANSFER | 14624 | 931 | 0.130% | 1.286% | 49.2% |

### FPR por tramo de monto

| Tramo | Legítimas | Fraudes | FPR bloqueo | FPR revisión o bloqueo | Recall rev.+bloq. |
|---|---|---|---|---|---|
| [0, 1000) | 4118 | 17 | 0.000% | 0.097% | 17.6% |
| [1000, 10000) | 31109 | 36 | 0.006% | 0.100% | 27.8% |
| [10000, 100000) | 42575 | 334 | 0.009% | 0.702% | 30.5% |
| [100000, 1e+06) | 43104 | 876 | 0.028% | 1.341% | 35.2% |
| [1e+06, inf) | 2222 | 599 | 0.045% | 0.495% | 79.1% |

### Frente a no tener modelo

Todo aprobado cuesta 2,979,091,950 (recall 0). El modelo cuesta 713,132,559 (76.1% menos) y detecta el 48.2% de los fraudes en REVIEW o BLOCK.

## Explicabilidad

SHAP global del XGBoost (TreeExplainer, 20,000 filas de valid):

![SHAP global](figures/shap_global.png)

| Feature | Media \|SHAP\| |
|---|---|
| type_PAYMENT | 0.8891 |
| dst_seen_before | 0.5360 |
| type_CASH_OUT | 0.4035 |
| type_TRANSFER | 0.3132 |
| log_amount | 0.2414 |
| hour_cos | 0.2201 |
| amount | 0.1677 |
| hour_sin | 0.1346 |
| dst_age_hours | 0.0507 |
| dst_in_sum_24h | 0.0290 |

## Ablación: conjunto B (con saldo), solo valid

| Modelo | PR-AUC A | PR-AUC B | Recall@0,5 % A | Recall@0,5 % B |
|---|---|---|---|---|
| logreg | 0.3595 | 0.5925 | 0.4000 | 0.5234 |
| xgb | 0.5626 | 0.9911 | 0.5320 | 1.0000 |

Mayores ganancias del XGBoost con B: {'amount_to_balance': 0.8203272819519043, 'src_balance_before': 0.10046707093715668, 'type_PAYMENT': 0.03145136311650276, 'amount': 0.012537729926407337, 'type_CASH_OUT': 0.006349943578243256}.

El PR-AUC de B es ≥ 0.95: es el artefacto del simulador descrito en §5b (el fraude vacía la cuenta) y B no se sirve.

## Limitaciones

- Datos sintéticos (PaySim): las cifras no se extrapolan a clientes reales.
- El fraude solo ocurre en TRANSFER y CASH_OUT; el modelo no ha visto fraude en PAYMENT ni DEBIT.
- Cambio de régimen en el volumen alrededor del paso 400 (ver `eda.md`): la prevalencia de fraude (sobre las filas de los cuatro tipos del modelo, TRANSFER, CASH_OUT, PAYMENT y DEBIT; sin CASH_IN) pasa de 0,099 % en train a 0,976 % en valid y 1,490 % en test; los umbrales fijados en valid no se trasladan limpiamente.
- Pocos bloques de pasos: 6 en valid con bloques de 24 pasos; los intervalos por bootstrap de bloques son poco fiables por debajo de 10.
- Casi todos los orígenes aparecen una sola vez: `src_out_*` e `is_new_beneficiary` casi no aportan información en PaySim.
- El costo está dominado por el monto de los fraudes aprobados; los costos unitarios son supuestos (sensibilidad ±50 %).
