# Registro de incidentes (STOP resueltos sin esperar decisión)

Autorización del usuario (2026-11-19, 03:15 a 07:50, hora local UTC−5): resolver los errores que normalmente dejarían
la corrida en STOP, documentando el error y la solución. Un archivo por incidente: `AAAAMMDD-HHMM-<tema>.md`, con los
campos de abajo en este orden, para que otro agente los procese. Las cifras son medidas, no estimadas.

```text
# <título corto>
- id: AAAAMMDD-HHMM-<tema>
- paso: <1..6 del plan de cierre de la fase 5>
- estado: resuelto | resuelto parcialmente | sin resolver
- tipo: configuración | código | datos | entorno | prueba
- límite del plan que tocó: <ninguno | p. ej. «cambio de código» o «plazo de la saga»>
## Error
<qué pasó, con comando, salida y cifras>
## Causa
<causa comprobada y cómo se comprobó>
## Solución
<qué se cambió (archivos, commit) y por qué esta opción>
## Verificación
<comando y resultado después del cambio>
## Pendiente para el usuario
<decisión que sigue abierta, o «nada»>
```

## Índice

| id | paso | estado | tipo | resumen |
|---|---|---|---|---|
| 20261119-0335-reconcile-full-acumulado | 2 | resuelto | prueba | (c) de reconcile-full suma las cargas anteriores en la misma base; no es error del sistema |
| 20261119-0334-reinicio-wsl | 2 | resuelto | entorno | dos pantallazos azules (0x139, 0x119 del driver AMD) a mitad de E1; recuperación sin descuadres y E1 repetido |
| 20261119-0450-compensacion-sin-medir | 3 | resuelto parcialmente | prueba | reconcile-full no medía la compensación; medida: 77,8 % < 60 s bajo el pico (objetivo 99 %) |
| 20261119-0557-kind-service-links | 5 | resuelto | configuración | POSTGRES_PORT=tcp://… inyectado por el Service rompía la URL JDBC; enableServiceLinks: false |
