# ADR-07 · Features online desde la bitácora de scoring

- Contexto: las features de velocidad necesitan historial reciente y consistente.
- Opciones: proyección alimentada por Kafka; Redis; la tabla `risk.scoring_log` que el servicio escribe en cada solicitud.
- Decisión: `scoring_log`, con dos consultas index-only concurrentes.
- Consecuencias: sin retraso de consumidor ni componente nuevo; la misma tabla alimenta PSI y reentrenamiento. Cuenta intentos, igual que el dataset de entrenamiento. No escala a miles de TPS; ahí entraría Redis.
