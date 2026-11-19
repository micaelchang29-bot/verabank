# kind-chaos no informaba el éxito, el FALLBACK ni el reemplazo: leía un formato de resumen de k6 que no existe
- id: 20261119-0604-kind-chaos-resumen
- paso: 5 (kind)
- estado: resuelto
- tipo: código (script de la prueba)
- límite del plan que tocó: ninguno
## Error
`make kind-chaos` (05:59) corrió k6 entero y borró el pod, pero salió con `KeyError: 'metrics'` y, por `set -e`, no imprimió
el FALLBACK de la ventana ni el tiempo de reemplazo.
## Causa
`scripts/kind-chaos.sh` leía el resumen estándar de k6 (`metrics.<métrica>.values.count`), pero `perf/transfers.js`
escribe uno propio con `handleSummary` (`counts`, `load_latency_ms`). El tiempo de reemplazo se calculaba bien, pero se
imprimía después de ese fallo.
## Solución
Leer `counts` y `load_latency_ms`; imprimir el tiempo de reemplazo apenas se mide y desactivar `set -e` tras la carga
para informar todo aunque una parte falle.
## Verificación
Corrida 1 (05:59), leída a mano: 4.891 solicitudes, 100 % de éxito, p95 20,2 ms. Corrida 2 (06:04) con el script
corregido: 4.906 respuestas, 100 % de éxito, p95 18,5 ms, p99 21,2 ms; FALLBACK 0 de 4.800; reemplazo Ready en 7 s;
nodo con 5,50 GiB.
## Pendiente para el usuario
Nada.
