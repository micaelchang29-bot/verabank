# ADR-02 · Outbox con relay por sondeo

- Contexto: cambiar estado y publicar el evento debe ser atómico.
- Opciones: publicar directo tras el commit; outbox + sondeo; outbox + CDC (Debezium).
- Decisión: outbox + sondeo. Un solo relay activo mediante bloqueo consultivo; lotes de 200 cada 100 ms.
- Consecuencias: sin eventos perdidos ni fantasma; entrega at-least-once y hasta 100 ms de retraso. Debezium reduce la latencia pero añade Kafka Connect a la laptop.
