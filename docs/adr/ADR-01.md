# ADR-01 · Saga orquestada

- Contexto: tres participantes (libro mayor, scoring, rail) y compensaciones con orden estricto. Dos cuentas en la misma base no necesitan saga.
- Opciones: orquestada; coreografiada por eventos; transacción local.
- Decisión: orquestada, con estado en `core.saga_state`. En transferencias internas el abono es un paso local y atómico de la misma máquina de estados.
- Consecuencias: flujo legible y testeable; la saga se justifica por el tramo externo. El orquestador concentra la lógica. Un timeout del rail no equivale a fallo: se consulta antes de compensar.
