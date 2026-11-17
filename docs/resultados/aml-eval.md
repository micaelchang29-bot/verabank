# Evaluación de las reglas AML

Contra la verdad de terreno de `amlgen` (D12). Solo cuentan las alertas que tocan transferencias de `data/aml/`; `BLOCK` y `REVIEW` se informan aparte. No se ajustó ninguna regla ni parámetro del generador para mejorar estas cifras.

- Semilla del generador: 42
- Transferencias de `data/aml/`: 30683
- Alertas en `risk.aml_alerts`: 13997; tocan estas transferencias: 86; excluidas por no tocarlas: 13911
- Alertas por cada 1.000 transferencias: 2,80
- Transferencias del archivo sin id de servicio (no creadas): 0

## Reglas

| Regla | Alertas | Tocan una instancia de su tipología | Precisión |
|---|---:|---:|---:|
| `STRUCTURING` | 35 | 35 | 100,00 % |
| `COUNTRY_RISK` | 0 | 0 | 0,00 % |

### `THRESHOLD` y otras reglas (solo conteo)

| Regla | Alertas | Tocan alguna instancia | Proporción |
|---|---:|---:|---:|
| `THRESHOLD` | 51 | 46 | 90,20 % |

## Tipologías

| Tipología | Instancias | Detectadas | Recall | Techo de cobertura | Estado |
|---|---:|---:|---:|---:|---|
| `STRUCTURING` | 40 | 25 | 62,50 % | 62,50 % | consistente |
| `COUNTRY_RISK` | 60 | 0 | 0,00 % | 65,00 % | consistente |
| `FAN_IN` | 20 | 0 | 0,00 % (por diseño) | — | no cubierta |
| `FAST_PASS` | 30 | 0 | 0,00 % (por diseño) | — | no cubierta |

`FAN_IN` (fuera del corte de alcance) y `FAST_PASS` (sin regla en §5a) no están cubiertas: recall 0 por diseño (manual 0.1). Si el recall de una tipología supera su techo, queda «inconsistente»: la implementación de la regla y la copia mínima del generador discrepan, y hay que investigarlo.

## Instancias afectadas por transferencias rechazadas o sin completar (aparte)

| Tipología | Con alguna rechazada por `BLOCK` | Con alguna sin completar |
|---|---:|---:|
| `STRUCTURING` | 1 | 0 |
| `COUNTRY_RISK` | 17 | 0 |
| `FAN_IN` | 1 | 0 |
| `FAST_PASS` | 12 | 0 |
