# ADR-06 · Claves y particiones de Kafka

- Contexto: AML necesita orden por cuenta; la saga, por transferencia.
- Opciones: `transfer_id` en todo; cuenta de origen en eventos de negocio.
- Decisión: `transfer.events` con clave `source_account_id` y 6 particiones; tópicos del rail con clave `transfer_id` y 3. Un tópico por agregado con varios tipos de evento.
- Consecuencias: orden por cuenta de origen. Las reglas por destino (fan-in) no tienen orden global: se evalúan por ventana de tiempo de negocio con upsert idempotente. Replicación 1 en la laptop; en producción, 3 con `min.insync.replicas=2`.
