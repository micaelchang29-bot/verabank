# En kind los servicios no arrancan: Kubernetes inyecta POSTGRES_PORT=tcp://… y rompe la URL JDBC
- id: 20261119-0557-kind-service-links
- paso: 5 (kind)
- estado: resuelto
- tipo: configuración
- límite del plan que tocó: ninguno (manifiestos)
## Error
`make kind-deploy`: «deployment "transfer-service" exceeded its progress deadline». transfer-service, risk-scoring y
risk-stream en CrashLoopBackOff (6 reinicios); los Jobs db-migrate y bootstrap completos. Log de transfer-service:
`Driver org.postgresql.Driver claims to not accept jdbcUrl, jdbc:postgresql://postgres:tcp://10.96.234.131:5432/verabank`.
## Causa
Kubernetes añade a cada pod variables de entorno al estilo de los enlaces de Docker por cada Service del espacio de
nombres: el Service `postgres` produce `POSTGRES_PORT=tcp://<ip>:5432` (y `kafka`, `KAFKA_PORT=…`). Los servicios leen
`POSTGRES_PORT` como puerto (`${POSTGRES_PORT:5432}`), así que la URL quedó con el valor del enlace. En Compose no pasa.
La comprobación estática no podía detectarlo: no había regla para ello.
## Solución
`enableServiceLinks: false` en las 9 plantillas de pod de `deploy/kind/base` (Deployments, StatefulSets y Jobs), y una
regla nueva en `deploy/kind/check_manifests.py` que lo exige en todo pod.
## Verificación
`make kind-check`: OK (7,88 GiB de límites). Redespliegue sobre el mismo clúster con `make kind-deploy`: ver
`docs/resultados/fase-5.md` (kind).
## Pendiente para el usuario
Nada.
