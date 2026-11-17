# ADR-09 · Una instancia de PostgreSQL, esquemas por servicio

- Contexto: memoria limitada.
- Opciones: instancia por servicio; una instancia con esquemas y roles.
- Decisión: una instancia. Ningún servicio lee el esquema de otro; solo las vistas de `analytics` cruzan esquemas, para BI.
- Consecuencias: ahorra memoria; el aislamiento es lógico, no físico.
