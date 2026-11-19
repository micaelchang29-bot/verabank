# reconcile-full marca diferencia al encadenar dos cargas en la misma base
- id: 20261119-0335-reconcile-full-acumulado
- paso: 2 (diagnóstico del rail)
- estado: resuelto (sin cambio de código; no es un error del sistema)
- tipo: prueba
- límite del plan que tocó: ninguno
## Error
`make load-transfers RATE=100 DURATION=5m`, segunda carga sobre la misma base (la primera fue RATE=50): `reconcile-full`
sale con 1 por `diferencia_k6_frente_a_filas_loadtest = 18001 (filas 54002; k6: 201 = 36001, con los 422 que crean
fila = 36001)`. Las demás comprobaciones en 0 y `verify-audit` íntegra (153.480 filas).
## Causa
La comprobación (c) compara todas las filas del canal `loadtest` de `core.transfers` con lo que reporta la última
corrida de k6. Con dos cargas en la misma base, las filas son acumuladas: 54.002 = 18.001 (carga de 50 TPS) + 36.001
(carga de 100 TPS). La diferencia es exactamente la carga anterior.
## Solución
Ninguna en el código: T-17 corre sobre base limpia (D10), como exige el plan, y ahí la comprobación es correcta. En las
cargas encadenadas del diagnóstico la cifra (c) se descuenta a mano con la carga anterior; las otras comprobaciones
son válidas tal cual.
## Verificación
18.001 filas de la primera carga (reconcile-full de esa corrida: diferencia 0) + 36.001 de la segunda = 54.002.
## Repetición (paso 3)
`make pg-restart-test` sobre la base de la corrida de 30 min: sus 449 transferencias (canal `loadtest`) se suman a las
108.002 de k6 → `diferencia = 449` (filas 108.451). Mismo artefacto; cuentas, retenciones y asientos en 0.

## Pendiente para el usuario
Nada. Si se quiere encadenar cargas, `reconcile-full` podría aceptar un instante de inicio; no se hizo.
