# BI: KPIs y Metabase (fase 5, HU-09, D9)

| Archivo | Qué es |
|---|---|
| `docs/kpis.sql` | Las cinco consultas de `docs/diseno.md` §5g, **textuales**, con sus marcadores `-- KPI n:` (una prueba lo compara con el diseño). |
| `docs/kpis-extra.sql` | `-- EXTRA A:` drift activo (alertas de `analytics.drift_metrics` de las dos últimas ventanas, por PSI: es el «aviso de drift» de §5f) y `-- EXTRA B:` cola de revisión con las tres razones de `analytics.fraud_reasons`. |
| `bi/setup_metabase.py` | Crea por la API, idempotente: administrador (primer arranque), conexión `VeraBank` con `verabank_bi` (el `bi_ro` del manual) y sincronización de esquema, colección `VeraBank`, siete tarjetas SQL nativas y el dashboard `VeraBank — Riesgo` (drift arriba, KPI 1–5, cola de revisión). |
| `bi/check_cards.py` | Consulta cada tarjeta con `POST /api/card/:id/query` e imprime filas y las tres primeras. |
| `db/scripts/create-metabase-db.sh` | Base de aplicación `metabase` y rol `metabase_app` en la misma instancia (idempotente). |

```bash
make bi-up        # perfil bi, bi-init y el script; imprime la URL del dashboard
make bi-check     # filas de cada tarjeta
make bi-url
make test-bi      # parser e idempotencia con la API simulada (respx)
```

- **Versión fijada**: `metabase/metabase:v0.64.6.3`, edición open source, línea LTS 64 (verificada el 2026-11-17 en
  <https://www.metabase.com/version-support> y en las etiquetas de Docker Hub). Nunca `latest`.
- **Memoria**: `JAVA_OPTS=-Xmx1g`, `mem_limit` 1536m; healthcheck `/api/health`; puerto `127.0.0.1:3000`.
- **Credenciales** en `.env` (`METABASE_DB_PASSWORD`, `METABASE_ADMIN_EMAIL`, `METABASE_ADMIN_PASSWORD`); ninguna en el
  repositorio. El correo del administrador es ficticio y local.
- **Separación**: `metabase_app` solo se conecta a la base `metabase`; Metabase lee los datos con `verabank_bi`, que no
  tiene privilegios en `core` (la vista cruza esquemas con los permisos de su dueño). La conexión filtra los esquemas
  `analytics` y `risk`.
- **Aviso de drift**: tarjeta del dashboard, no correo (exigiría SMTP).
- **Idempotencia**: cada objeto se busca por nombre (y colección) y se actualiza; una segunda ejecución no crea nada.
  Las tarjetas del dashboard conservan su id entre ejecuciones.
- Las pruebas SQL de los KPIs (valores calculados a mano sobre 12 transferencias) están en `db/tests/test_kpis.py`.
