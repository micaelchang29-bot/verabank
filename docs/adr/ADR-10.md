# ADR-10 · Versión de Spring Boot

- Contexto: el stack pide Spring Boot 3, pero la línea 3.5 dejó de recibir parches open source el 30 de junio de 2026.
- Opciones: 3.5.x; 4.x.
- Decisión: 3.5.x, último parche, evitando APIs retiradas en 4.x.
- Consecuencias: respeta el stack, pero publicar sobre una línea sin soporte invita preguntas. Primera decisión a revisar (§8).

## Resolución

Spring Boot 3.5.16, el último parche de la línea. El caso fija Spring Boot 3 y los prompts de §7 están escritos para esa línea; el proyecto corre en local, sin exposición. La migración a 4.1 se declara en el README como primer cambio para producción.
