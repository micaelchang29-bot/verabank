# ADR-08 · Un servicio Quarkus, dos despliegues

- Contexto: el stack fija un único servicio Quarkus, pero scoring y AML tienen cargas distintas.
- Opciones: un proceso con todo; misma imagen con perfil `scoring` o `stream`.
- Decisión: dos perfiles.
- Consecuencias: recursos aislados y escalado independiente sin duplicar código; exige disciplina de paquetes para separarlo después.

## Resolución

Un único `risk-service` con dos perfiles (`scoring` y `stream`): el stack fija un único servicio Quarkus, y así hay una sola imagen y un solo build.
