# ADR-03 · Idempotencia en tres capas

- Contexto: reintentos del cliente, reentregas de Kafka y reintentos de la saga.
- Opciones: tabla de claves con respuesta almacenada; clave como restricción única de la transferencia.
- Decisión: la segunda. API: `UNIQUE (client_id, idempotency_key)` más `request_hash`; toda solicitud válida crea una fila aunque se rechace. Consumidores: `processed_messages` en la misma transacción que el efecto. Libro mayor: unicidad por transferencia, cuenta, sentido y tipo.
- Consecuencias: la restricción resuelve la carrera entre duplicados concurrentes; el reintento devuelve el estado actual, no una copia de la respuesta original.
