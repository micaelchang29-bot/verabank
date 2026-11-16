# Base de datos

Una instancia de PostgreSQL con un esquema por servicio (ADR-09). El DDL es el de
`docs/diseno.md` §4, repartido sin cambios en una sola copia por migración.

| Directorio | Ejecuta | Rol | Versiones |
|---|---|---|---|
| `migrations/core` | transfer-service | `verabank_transfer` | V1–V8 |
| `migrations/audit` | transfer-service | `verabank_transfer` | V9–V12 |
| `migrations/risk` | risk-service | `verabank_risk` | V1–V4 |
| `migrations/analytics` | job de la fase 5 | rol de arranque (`POSTGRES_USER`) | V1 (solo `CREATE SCHEMA`) |

`core` y `audit` las ejecuta el mismo servicio en una sola corrida de Flyway, así que sus
números de versión no se repiten entre los dos directorios.

## Configuración de Flyway por servicio

Cada servicio lleva su propio historial en un esquema aparte (`flyway_transfer`,
`flyway_risk`, `flyway_analytics`), porque los esquemas de negocio aún no existen cuando
Flyway crea la tabla de historial y cada migración empieza con su `CREATE SCHEMA`.

```text
transfer-service: defaultSchema=flyway_transfer  locations=db/migrations/core,db/migrations/audit
risk-service:     defaultSchema=flyway_risk      locations=db/migrations/risk
analytics (job):  defaultSchema=flyway_analytics locations=db/migrations/analytics
```

Los roles (`db/init/roles.sql`) tienen `CREATE` sobre la base, de modo que el rol que ejecuta
cada migración queda como propietario de su esquema. Ningún rol tiene privilegios sobre el
esquema de otro. `verabank_bi` solo lee lo que cree el rol de arranque, es decir, `analytics`.

Quedan sin crear, por no tener DDL en §4: `risk.transfer_facts`, `risk.aml_rules`,
`risk.country_risk`, la inbox de `risk`, `analytics.credit_scores` y
`analytics.drift_metrics`.

`analytics.v_transfer_outcomes` sí tiene DDL en §4, pero se crea en la fase 5: la vista
cruza `core` (`transfers`, `accounts`, `customers`) y `risk` (`fraud_labels`), así que solo
puede crearse cuando ambos esquemas existan, con el rol de arranque, que es quien crea
`analytics`.

## Datos semilla

`db/seed/` solo es para entornos locales y no forma parte de las migraciones.
