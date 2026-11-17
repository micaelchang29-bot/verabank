# ADR-04 · Scoring síncrono con fallback

- Contexto: hay que puntuar antes de aprobar.
- Opciones: REST síncrono; petición y respuesta por Kafka; asíncrono tras aprobar.
- Decisión: REST síncrono con timeout de 120 ms y circuit breaker. Ante fallo no se rechaza ni se aprueba todo: monto ≤ 500 aprueba, mayor pasa a `REVIEW`, con `decision_source = FALLBACK`.
- Consecuencias: latencia acotada y medible; el banco sigue operando con riesgo limitado y la tasa de fallback es un KPI. Kafka añadiría dos saltos al p95; el asíncrono incumple el requisito.

## Resolución

Límite de fallback fijado en 500: hasta 500 aprueba y un monto mayor pasa a `REVIEW`. Fallar cerrado incumple el NFR de 99 % de solicitudes aceptadas con el scoring caído (§1.2). El límite queda parametrizado.
