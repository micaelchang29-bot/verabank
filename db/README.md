# Base de datos

Una instancia de PostgreSQL con un esquema por servicio (ADR-09). El DDL es el de
`docs/diseno.md` §4, repartido sin cambios en una sola copia por migración.

| Directorio | Ejecuta | Rol | Versiones |
|---|---|---|---|
| `migrations/core` | transfer-service | `verabank_transfer` | V1–V8 |
| `migrations/audit` | transfer-service | `verabank_transfer` | V9–V13 |
| `migrations/risk` | risk-service (hasta la fase 4, `make migrate-risk`) | `verabank_risk` | V1–V10 |
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

Quedan sin crear, por no tener DDL en §4: `analytics.credit_scores` y `analytics.drift_metrics`.

`analytics.v_transfer_outcomes` sí tiene DDL en §4, pero se crea en la fase 5: la vista
cruza `core` (`transfers`, `accounts`, `customers`) y `risk` (`fraud_labels`), así que solo
puede crearse cuando ambos esquemas existan, con el rol de arranque, que es quien crea
`analytics`.

## Tablas de apoyo de `risk` (V5–V10)

El DDL de §4 se repartió en la fase 1 (`scoring_log`, `fraud_labels`, `aml_alerts`: V2–V4, sin cambios).
Las tablas de apoyo, sin DDL en §4, son:

| Tabla | Migración | Para qué |
|---|---|---|
| `risk.transfer_facts` | V5 | Proyección de `TransferCompleted` para las ventanas AML. Índices `(source_account_id, occurred_at DESC) INCLUDE (amount)` y `(dest_ref, occurred_at DESC) INCLUDE (amount, source_account_id)` (este último, para `FAN_IN`). |
| `risk.aml_rules` | V6 | Parámetros versionados: PK `(rule_code, version)` e índice único parcial, una sola versión activa por regla. Un disparador impide editar `params`, `version` y `valid_from`: un cambio de parámetros es una versión nueva (INSERT) con la anterior en `active = false`, nunca un UPDATE de los parámetros. |
| `risk.country_risk` | V7 | País (ISO 3166-1 alfa-2), nivel `HIGH` o `WATCH`, URL de la lista, `list_date` y vigencia `[valid_from, valid_to)`; PK `(country, valid_from)`. |
| `risk.aml_exempt_accounts` | V8 | Cuentas que no generan alertas AML (hoy, `CLEARING`). |
| `risk.processed_messages` | V9 | Inbox de `risk`, igual que `core.processed_messages`. |

`audit.append(actor, action, entity_type, entity_id, correlation_id, detail)` (V13) es el único punto por el que
otro servicio escribe en `audit.audit_log`. Es `SECURITY DEFINER` con `search_path = pg_catalog, pg_temp`, devuelve
el `audit_id` y lo ejecuta con los privilegios de `verabank_transfer`; el trigger de cadena (V11) asigna
`audit_id`, `prev_hash` y `row_hash`. `EXECUTE` está revocado a `PUBLIC`. No concede nada a `risk_svc`, porque las
migraciones de audit también corren donde ese rol no existe (pruebas de transfer-service): el `GRANT` va en
`db/init/grants.sql`.

## Roles y permisos

| Rol | Quién lo usa | Permisos |
|---|---|---|
| `verabank_transfer` | transfer-service | Dueño de `core` y `audit`. |
| `verabank_risk` | migraciones de `risk` | Dueño de `risk`. Sin acceso a `core` ni `audit`. |
| `risk_svc` | risk-service en ejecución | `SELECT/INSERT/UPDATE/DELETE` en todas las tablas de `risk` (también las futuras, por `ALTER DEFAULT PRIVILEGES FOR ROLE verabank_risk`); `EXECUTE` en `audit.append` y `USAGE` del esquema `audit`, nada más. No es dueño de ningún objeto. Sin acceso a `core`. |
| `sim_loader` | calentamiento del replay (D1) | Solo `INSERT` en `risk.scoring_log`. No lee ni modifica nada. |
| `verabank_bi` | BI | Solo `SELECT` en `analytics` (privilegios por defecto de `roles.sql`) y en `risk` (tablas actuales y futuras). |

Los creados por `db/init/roles.sql` (`verabank_*`) existen desde la inicialización del volumen. `risk_svc` y
`sim_loader` los crea `db/init/roles_runtime.sql`, idempotente y con el superusuario de arranque: lo ejecuta
`db/init/initdb.d/20-roles-runtime.sh` en los volúmenes nuevos y `make db-roles` en los que ya existen. Las
contraseñas llegan del entorno (`.env`): `RISK_SVC_PASSWORD` y `SIM_LOADER_PASSWORD`, sin valor en
`.env.example`, sin imprimirse y sin pasar por la línea de comandos.

Los permisos sobre objetos (`db/init/grants.sql`) son idempotentes y se aplican con `make db-grants`
**después** de las migraciones de `core`, `audit` y `risk`.

Orden de `make up-core`: roles de ejecución → transfer-service (migra `core` y `audit`) → `make migrate-risk` →
`make db-grants` → semilla de `core` → `make seed-risk` → `make load-country-risk`. Cada paso se puede repetir.

`make migrate-risk` ejecuta Flyway (imagen `flyway/flyway:11.7.2`, la misma versión que transfer-service) con
`verabank_risk`, `defaultSchema=flyway_risk` y `db/migrations/risk`: es la configuración que usará risk-service
cuando exista (fase 4).

## Datos semilla

`db/seed/` y los objetivos `make seed-*` son para entornos locales. Las **reglas AML** v1 son una migración
(`risk/V10`), porque son datos de configuración versionados:

| Regla | Parámetros v1 |
|---|---|
| `THRESHOLD` | `{"threshold": "10000.00"}` |
| `STRUCTURING` | `{"threshold": "10000.00", "min_ratio": 0.70, "max_ratio": 1.00, "min_count": 3, "window_hours": 24, "min_total": "10000.00"}` (`max_ratio` es exclusivo) |
| `COUNTRY_RISK` | `{"high_min_amount": "1000.00", "watch_min_amount": "5000.00"}` |

No hay `FAN_IN` (manual 0.1): se añadirá como versión 1 de esa regla cuando se active.

- `db/seed/01_core_seed.sql` (`db/seed/load.sh`): cuenta `CLEARING`, 30 clientes y sus cuentas.
- `make seed-risk`: inserta `CLEARING` en `risk.aml_exempt_accounts`. `risk` no puede leer `core`, así que el id se
  lee de la semilla de la fase 1 (`md5('verabank-seed:clearing')::uuid`) y se calcula en el script. Idempotente.
- `make load-country-risk [CSV=ruta]`: carga `db/seed/country_risk.csv` con `valid_from = list_date`. El CSV es una
  instantánea completa con una sola `list_date`. Al cargar una lista más nueva se cierra (`valid_to = list_date`)
  toda vigencia anterior, también la de los países que salieron; una lista más antigua que la vigente se rechaza.
  Repetir la misma carga no cambia nada.

### Lista de países de riesgo

`db/seed/country_risk.csv` (columnas `country,level,source,list_date`; 21 filas) recoge las dos listas del GAFI/FATF
publicadas el **2026-10-30**:

- `HIGH`, «High-Risk Jurisdictions subject to a Call for Action» (3 países):
  <https://www.fatf-gafi.org/en/publications/High-risk-and-other-monitored-jurisdictions/call-for-action-october-2026.html>
- `WATCH`, «Jurisdictions under Increased Monitoring» (18 países):
  <https://www.fatf-gafi.org/en/publications/High-risk-and-other-monitored-jurisdictions/increased-monitoring-october-2026.html>

**Procedencia.** Las páginas del GAFI devuelven 403 a clientes automáticos, así que la lista no se descargó: se
transcribió de la que facilitó la persona responsable del proyecto, que la obtuvo de una fuente secundaria. Está
**pendiente de contraste con las dos páginas del GAFI**. Mapeo a alfa-2: Iran `IR`, DPRK `KP`, Myanmar `MM`;
Angola `AO`, Bolivia `BO`, Bosnia and Herzegovina `BA`, Cameroon `CM`, Haiti `HT`, Iraq `IQ`, Kenya `KE`, Kuwait `KW`,
Lao PDR `LA`, Lebanon `LB`, Nepal `NP`, Papua New Guinea `PG`, South Sudan `SS`, Syria `SY`, Venezuela `VE`,
Vietnam `VN`, Virgin Islands (UK) `VG` (no `VI`, que son las de EE. UU.), Yemen `YE`.

## Pruebas

`make test-db` (`db/tests`, entorno propio en `db/pyproject.toml` y `db/uv.lock`; necesita Docker; job `db` de la
CI). Levanta PostgreSQL 16.16 desechable con los scripts de `db/init`, aplica con Flyway lo mismo que `make up-core`
y ejecuta los scripts de `scripts/` contra ese contenedor. Cubre: migraciones desde cero y dos veces seguidas;
`core` y `audit` migran en una base sin `risk_svc`; volumen nuevo y existente quedan iguales; una sola versión activa
por regla y parámetros inmutables; `risk_svc` no lee `core` ni inserta en `audit.audit_log`, pero `audit.append`
inserta y encadena el hash; `sim_loader` solo inserta en `scoring_log`; BI solo lee; `make seed-risk` y
`make load-country-risk` idempotentes; el CSV sin duplicados y solo con códigos alfa-2 reales.
