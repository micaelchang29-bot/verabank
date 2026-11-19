# reconcile-full no medía la compensación de las FAILED; medida, no cumple el 99 % < 60 s bajo el pico
- id: 20261119-0450-compensacion-sin-medir
- paso: 3 (T-17)
- estado: resuelto parcialmente (medición corregida; el objetivo no se cumple)
- tipo: prueba
- límite del plan que tocó: ninguno (consulta de verificación, no código de servicio)
## Error
`make load-transfers DURATION=30m PEAK=1` pasó, pero `reconcile-full` informó «compensación de las FAILED = sin
FAILED» con 7.347 transferencias FAILED en la base.
## Causa
La consulta informativa de `db/reconcile_full.sql` filtraba `completed_at IS NOT NULL`; `TransferRepository` solo fija
`completed_at` al pasar a COMPLETED, así que ninguna FAILED entraba y el objetivo de §1.2 nunca se medía.
## Solución
Medir desde `core.transfers.created_at` hasta el `updated_at` de su saga en `COMPENSATED` (cierre de S10), con el
porcentaje bajo 60 s.
## Verificación
Misma base, tras la corrida de 30 min con pico: p50 0,250 s, p95 181,4 s, p99 197,1 s; n = 7.347; < 60 s: 77,79 %.
## Pendiente para el usuario
El objetivo de §1.2 (99 % < 60 s) no se cumple bajo el pico de 100 TPS: los rechazos del rail esperan en
`rail.replies.v1` detrás del techo de la cadena de auditoría (docs/resultados/diagnostico-rail.md). Ningún ajuste de
configuración lo mueve; requiere una de las decisiones de diseño del diagnóstico.
