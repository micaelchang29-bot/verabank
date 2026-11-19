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
