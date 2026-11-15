# VeraBank — Manual de construcción

Lista de tareas en orden de ejecución. Complementa a `verabank-diseno.md` y no repite su contenido.

**Convenciones**

- `«inicio del texto…» (§n)`: copiar el bloque completo desde esa sección del diseño.
- **[FALTA]**: el diseño lo exige pero no lo define; hay que redactarlo o decidirlo antes de avanzar.
- **(ampliación)**: fuera de la línea de corte de §8.
- Una fase se cierra solo cuando pasa su aceptación.

---

## 0. Antes de escribir código

### 0.1 Decisiones tomadas (§8, «Decisiones a revisar primero»)

| Decisión | Resolución | Motivo |
|---|---|---|
| ADR-10 | Spring Boot 3.5.16, último parche de la línea | El caso fija Spring Boot 3 y los prompts de §7 están escritos para esa línea; el proyecto corre en local, sin exposición. La migración a 4.1 se declara en el README como primer cambio para producción. |
| Línea de corte | Fases 1–4; kind y crédito como ampliación | Es el corte que §8 propone para una persona. |
| Regla AML fuera del corte | `FAN_IN` | Es la única sin orden por clave (ADR-06). El generador conserva la tipología y su recall se reporta como no cubierto. |
| ADR-04 | Límite 500: hasta 500 aprueba, mayor pasa a `REVIEW` | Fallar cerrado incumple el NFR de 99 % de solicitudes aceptadas con el scoring caído (§1.2). El límite queda parametrizado. |
| ADR-08 | Un `risk-service` con dos perfiles | El stack fija un único servicio Quarkus; una imagen y un build. |
| Auditoría | Cadena de hash síncrona | La carga objetivo es 50 TPS con pico de 100 (§1.2), dentro de lo que §8 da por aceptable. |

- [ ] Anotar cada resolución en su ADR. (Features A o B se decide en la fase 3, tras el EDA.)

### 0.2 Verificaciones (hechas el 2026-11-15)

| Punto | Resultado | Efecto en el plan |
|---|---|---|
| PaySim, licencia | CC BY-SA 4.0 | Atribución en el README; el dataset y sus derivados no se suben al repositorio |
| PaySim, descarga | Anónima con GET (el HEAD responde 404); archivo de unos 0,5 GB | No hace falta cuenta de Kaggle |
| Spring Boot | 3.5.16 es el último parche; soporte open source vencido el 2026-06-30. 4.0.x lo tiene hasta el 2026-12-31 y 4.1.x hasta el 2027-07-31 | Se fija 3.5.16 (0.1) |
| Quarkus | LTS vigente 3.40, con mantenimiento hasta el 2027-09-30 | Se fija 3.40.x |
| XGBoost a ONNX | onnxmltools 1.16.0 declara soporte de XGBoost 3, sin mencionar la 3.4.1 | Prueba de humo al inicio de la fase 3 |
| ONNX Runtime para Java | 1.32.0 incluye binarios `win-x64` y `linux-x64` | Cubre el equipo y los contenedores |
| GitHub Actions | Gratuito en repositorios públicos; 6 h por job; runner de 4 CPU y 16 GB | La CI no entrena el modelo completo ni corre pruebas de carga |
| Metabase | La serialización de dashboards es de pago | Sin cambio: el prompt de la fase 5 ya los crea por API |
| Equipo | Windows 11, 31 GB de RAM, 6 núcleos y 12 hilos; Docker detenido, sin `.wslconfig`, Java 17 | Preparar el entorno (0.3) |

**Versiones fijadas**

| Componente | Versión |
|---|---|
| Java | 21 (Temurin) |
| Spring Boot | 3.5.16 |
| Quarkus | 3.40.x (LTS) |
| ONNX Runtime | 1.32.0, la misma en Java y en Python |
| onnxmltools | 1.16.0 |
| XGBoost | La que pase la prueba de humo de la fase 3: 3.4.1 y, si falla, 3.1.x |
| Python | 3.12 |

### 0.3 Entorno, cuentas y herramientas

El equipo es Windows 11. Todo el manual se ejecuta en una terminal de WSL2 (Ubuntu), con el repositorio dentro del sistema de archivos de WSL y no bajo `/mnt/c`.

- [ ] Cuenta de GitHub. (No se necesita cuenta de Kaggle.)
- [ ] `wsl -l -v` muestra una distro en versión 2; si no hay ninguna, `wsl --install -d Ubuntu`.
- [ ] Crear `%UserProfile%\.wslconfig` y reiniciar WSL con `wsl --shutdown`. Los valores son los del equipo de referencia de §1.3:

```ini
[wsl2]
memory=16GB
processors=8
```

- [ ] Iniciar Docker Desktop y activar su integración con la distro de WSL; `docker info` debe responder desde la terminal de WSL.
- [ ] Instalar dentro de WSL (el Java 17 de Windows no se usa):

| Herramienta | Desde la fase | Comprobación |
|---|---|---|
| Git | 1 | `git --version` |
| Java 21 (Temurin) | 1 | `java -version` |
| Make, `curl`, `unzip` | 1 | `make --version` |
| Cliente `psql` | 1 | `psql --version` |
| Python 3.12 | 2 (simulador) | `python3 --version` |
| k6 | 4 | `k6 version` |
| kind y kubectl | 5 (ampliación) | `kind version` |

---

## 1. Crear el proyecto

- [ ] Crear el repositorio público `verabank` en GitHub, vacío, y clonarlo dentro de WSL.
- [ ] Configurar la identidad propia: `git config user.name` y `git config user.email`.
- [ ] Crear la estructura (§7, Fase 1, primer ítem). Se añaden `db/`, `perf/` y `data/`, que los prompts y comandos usan pero esa lista omite:

```bash
mkdir -p contracts/events transfer-service risk-service ml simulator deploy/kind docs/adr \
         db/migrations/{core,risk,audit,analytics} perf data/raw .githooks
```

- [ ] `.gitignore`: `data/`, `mlruns/`, `mlflow.db`, `target/`, `.venv/`, `__pycache__/`, `.env`.
- [ ] `.gitattributes` con `* text=auto eol=lf`, para que los scripts no se rompan si el repositorio se clona desde Windows.
- [ ] Copiar el diseño a `docs/diseno.md` para que los prompts puedan citar secciones.
- [ ] `CONTRIBUTING.md` ← «Regla del repositorio (`CONTRIBUTING.md`, se añade a cada prompt): commits convencionales…» (§7).
- [ ] Hook que rechaza firmas y pies en los mensajes de commit:

```sh
#!/bin/sh
# .githooks/commit-msg
if grep -qiE 'co-authored-by:|generated with|-session:' "$1"; then
  echo "Rechazado: el mensaje contiene firmas o pies de herramientas." >&2
  exit 1
fi
```

```bash
chmod +x .githooks/commit-msg && git config core.hooksPath .githooks   # repetir en cada clon
```

- [ ] Si se usa un asistente de código: desactivar en su configuración la atribución automática en commits y pull requests antes del primer commit.
- [ ] `docs/adr/ADR-01.md` … `ADR-10.md` ← «ADR-01 · Saga orquestada…» (§2.5), un archivo por ADR, con las resoluciones de 0.1.
- [ ] `Makefile` vacío; los objetivos se añaden por fase:

| Fase | Objetivos |
|---|---|
| 1 | `up-core`, `test-core`, `demo-idempotency`, `reconcile` |
| 2 | `chaos-rail` |
| 3 | `data`, `ml-train`, `ml-export`, `credit-batch` |
| 4 | `up`, `replay`, `aml-eval` |
| 5 | `kind-deploy`, `smoke`, `drift-demo` |

- [ ] Primer commit `chore: estructura inicial del monorepo` y push.

**Cómo usar cada prompt de §7:** pegarlo completo, añadir al final la regla de `CONTRIBUTING.md`, las versiones fijadas de 0.2 y los puntos **[FALTA]** que la fase indique.

---

## 2. Fase 1 — Contratos y núcleo transaccional

### 2.1 Contratos

- [ ] `contracts/events/envelope.v1.json` ← «{ "$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "verabank/events/envelope.v1.json"…» (§3.2).
- [ ] `contracts/events/<EventType>.v1.schema.json`: un archivo por cada uno de los 14 `event_type` de la tabla de §3.3. Campos obligatorios salvo los marcados con `?`; propiedades adicionales permitidas; montos como cadena decimal.
- [ ] `contracts/openapi.yaml` ← tablas de rutas y de errores de §3.4 (errores en `application/problem+json`).
- [ ] `contracts/features.yaml` ← tabla de features de §5b (conjunto A servido; B marcado como ablación).

### 2.2 Infraestructura local

- [ ] `docker-compose.yml` con PostgreSQL 16+ y Kafka en KRaft con la imagen `apache/kafka` (§2.6). Usar perfiles de Compose para no levantar todo a la vez (§8).
- [ ] Script de creación de los tópicos de §3.1 con sus particiones y retención, más los `.dlt`.
- [ ] Migraciones Flyway: repartir sin modificar el DDL «CREATE SCHEMA core; CREATE SCHEMA risk; CREATE SCHEMA audit; CREATE SCHEMA analytics;…» (§4) en `db/migrations/<esquema>/`. Esta fase necesita `core` y `audit`.
- [ ] **[FALTA]** Quién ejecuta las migraciones de `audit` y `analytics`, y en qué orden (la vista de `analytics` cruza `core` y `risk`).
- [ ] **[FALTA]** Roles de base: uno por servicio, sin acceso al esquema de otro, y uno de solo lectura para BI (ADR-09, §1.3).
- [ ] **[FALTA]** Datos semilla: cuenta `CLEARING`, clientes y cuentas de prueba. El saldo inicial debe entrar como transferencia desde la cuenta `CLEARING`, con sus asientos; si se escribe directo en `balance`, `make reconcile` no devuelve 0.

### 2.3 transfer-service

- [ ] Generar el proyecto: Spring Boot 3.5.16, Java 21; web MVC, JDBC, Flyway, PostgreSQL, Kafka, Actuator, validación; Testcontainers para pruebas.
- [ ] Ejecutar el prompt «Contexto: monorepo `verabank`. Implementa `transfer-service` con Java 21 y Spring Boot 3.5…» (§7, Fase 1).
- [ ] **[FALTA]** Añadir al prompt lo que la lista de la fase pide y el prompt no cubre: `GET /api/v1/transfers/{id}` y `GET /api/v1/accounts/{id}` (§3.4), escritura en `audit.audit_log` (HU-07) y API key por canal que resuelve el `client_id` (§1.3).
- [ ] Revisar el resultado contra ADR-03 y el flujo `tx1` / `tx2` de §2.3.
- [ ] Flujo de CI: compilación y pruebas.
- [ ] Objetivos de Make de la fase; `reconcile` ← «SELECT count(*) AS cuentas_descuadradas FROM core.accounts a…» (§7, Conciliación).

### 2.4 Aceptación

- [ ] T-01 a T-04 en verde (§6).
- [ ] «make up-core && make test-core…» (§7, Fase 1).
- [ ] Cada transferencia completada tiene su evento en `transfer.events.v1`.
- [ ] `make reconcile` devuelve 0 y 0.
- [ ] Etiqueta `fase-1`.

---

## 3. Fase 2 — Saga externa y compensación

- [ ] `docs/saga.md` ← «POST /transfers (Idempotency-Key) tx1 insertar transferencia PENDING…» (§2.3) y ADR-01. El prompt de la fase lo exige.
- [ ] **[FALTA]** Prompt del rail simulado en `simulator/` (Python): consume `rail.commands.v1`, responde en `rail.replies.v1`; atiende `CreditInstruction`, `CreditStatusQuery` y `CreditCancel` (§3.3); tasa de rechazo, retraso y duplicados configurables.
- [ ] Ejecutar el prompt «`transfer-service` (Spring Boot 3.5, JDBC) ya resuelve transferencias internas con outbox. Añade la saga…» (§7, Fase 2).
- [ ] **[FALTA]** Añadir al prompt: métricas `saga.stuck` y `outbox.lag` (§1.2) y auditoría de la decisión del analista (HU-05).
- [ ] Objetivo `chaos-rail`.

**Aceptación**

- [ ] T-05 a T-10 en verde (§6).
- [ ] «make chaos-rail REJECT=0.2 DUP=0.1 N=2000…» (§7, Fase 2): todas las transferencias en estado final y conciliación en cero.
- [ ] Etiqueta `fase-2`.

---

## 4. Fase 3 — Ciencia de datos offline

Puede avanzar en paralelo con la fase 2 desde que existe `contracts/features.yaml`.

- [ ] Entorno Python 3.12 en `ml/` con las librerías que lista el prompt de la fase y las versiones de 0.2.
- [ ] Prueba de humo de conversión, antes que el resto: entrenar un XGBoost mínimo con la 3.4.1, convertirlo con onnxmltools 1.16.0 y comparar contra `predict_proba` con tolerancia 1e-5. Si falla, repetir con 3.1.x; si también falla, plan B de ADR-05 (XGBoost4J). Fijar en `ml/requirements.txt` el par que pase.
- [ ] `make data`: PaySim a `data/raw/paysim.csv` y Default of Credit Card Clients de UCI (§5a). PaySim se descarga sin cuenta:

```bash
curl -L -o data/raw/paysim1.zip https://www.kaggle.com/api/v1/datasets/download/ealaxi/paysim1
unzip -o data/raw/paysim1.zip -d data/raw/paysim && mv data/raw/paysim/*.csv data/raw/paysim.csv
```

- [ ] Cuaderno de EDA: volumen y prevalencia de fraude por `step`, fuga en las columnas de saldo. Ajustar los cortes para que cada partición tenga al menos 500 fraudes (§5c).
- [ ] `ml/config.yaml` ← cortes 1–400 / 425–550 / 575–744, hueco de 24 pasos, costos unitarios y límites de FPR (§5c). El prompt lo exige.
- [ ] Ejecutar el prompt «Proyecto Python 3.12 en `ml/` (pandas, scikit-learn, XGBoost, MLflow local…» (§7, Fase 3).
- [ ] Revisar contra los siete puntos de «Prevención de fuga» (§5b), los umbrales de §5c y el criterio de selección de §5d.
- [ ] Decidir conjunto de features A o B (§8, decisión 3) y anotarlo.
- [ ] Artefactos: `thresholds.json`, `model.onnx`, `golden.jsonl`.
- [ ] **[FALTA]** Decidir si esos tres artefactos se versionan: T-13 corre en la CI y los necesita.
- [ ] **[FALTA]** Informe SHAP global del modelo (§5e); el prompt no lo incluye.
- [ ] **[FALTA]** Prompt de `amlgen` ← «**Generador AML.** Fondo legítimo lognormal…» (§5a), con la verdad de terreno en un archivo aparte.
- [ ] **(ampliación) [FALTA]** Prompt de crédito: scorecard WoE frente a XGBoost monotónico (§5d), tres razones por cliente e informe de equidad (§5e), lote a `analytics.credit_scores` en menos de 5 min (§1.2).

**Aceptación**

- [ ] T-13 (lado Python) y T-14 en verde (§6).
- [ ] «make data && make ml-train && make ml-export…» (§7, Fase 3).
- [ ] MLflow muestra ambos modelos con PR-AUC, recall a FPR fija y costo.
- [ ] Etiqueta `fase-3`.

---

## 5. Fase 4 — risk-service e integración

- [ ] **[FALTA]** DDL de las tablas de apoyo «Tablas de apoyo no detalladas…» (§4): `risk.transfer_facts`, `risk.aml_rules`, `risk.country_risk` y el inbox de `risk`.
- [ ] Sembrar `risk.aml_rules` con la tabla «Reglas AML» (§5a), sin `FAN_IN` (0.1), y `risk.country_risk` con la lista del GAFI y su fecha de vigencia.
- [ ] Generar el proyecto Quarkus 3.40.x (LTS), Java 21, modo JVM, con ONNX Runtime 1.32.0.
- [ ] Copiar `model.onnx`, `thresholds.json` y `golden.jsonl` al servicio.
- [ ] Ejecutar el prompt «Crea `risk-service` con Quarkus 3 (REST reactivo, cliente PostgreSQL reactivo…» (§7, Fase 4), quitando fan-in de la cadena de reglas.
- [ ] **[FALTA]** Añadir al prompt: modelo en sombra (`shadow_score`), consumo de `fraud.labels.v1` hacia `risk.fraud_labels` y publicación en `aml.alerts.v1`.
- [ ] **[FALTA]** Decidir qué servicio expone `GET` y `PATCH /api/v1/aml/alerts` (§3.4 no lo asigna).
- [ ] **[FALTA]** Prompt de `RemoteScorer` en `transfer-service`: timeout de 120 ms, circuit breaker y fallback por monto con `decision_source = FALLBACK` (ADR-04); eventos `TransferScored` y `TransferHeld`.
- [ ] Compose: la misma imagen desplegada dos veces, `risk-scoring` y `risk-stream` (ADR-08).
- [ ] **[FALTA]** Prompt de `simulator replay` ← «**Replay de PaySim.** Se reproduce el período de prueba…» (§5a) y de `simulator aml`; etiquetas con 24 h simuladas de retraso (§1.3).
- [ ] `perf/scoring.js` para k6 con umbral `p(95)<150`.
- [ ] Objetivos `up`, `replay`, `aml-eval`.

**Aceptación**

- [ ] T-11 a T-13 y T-15 en verde (§6).
- [ ] «make up && make replay N=20000 TPS=50…» (§7, Fase 4).
- [ ] Con el scoring detenido, la API sigue aceptando con `decision_source = FALLBACK`.
- [ ] Informe de precisión y recall por tipología AML.
- [ ] Etiqueta `fase-4`. **Aquí termina la línea de corte.**

---

## 6. Fase 5 — BI, monitoreo, Kubernetes y CI

- [ ] Vista «CREATE VIEW analytics.v_transfer_outcomes AS…» (§4).
- [ ] **[FALTA]** DDL de `analytics.drift_metrics` y `analytics.credit_scores`.
- [ ] `docs/kpis.sql` ← «-- KPI 1: tasa de detección de fraude por día (casos y monto)…» (§5g), las cinco consultas. El prompt lo exige.
- [ ] Metabase en Compose con el rol de solo lectura. Los dashboards se crean por API; la edición open source no los exporta (0.2).
- [ ] Ejecutar el prompt «Repositorio con servicios ya funcionales en Docker Compose. Entrega: (1) manifiestos Kustomize…» (§7, Fase 5).
- [ ] Dimensionar la CI para el runner estándar (4 CPU, 16 GB): modelo ya entrenado o muestra de 50.000 filas; T-17 y las pruebas de caos se quedan en local.
- [ ] **[FALTA]** Verificador de la cadena de hash para T-16 (HU-07).
- [ ] **[FALTA]** Script k6 de carga para T-17: 50 TPS durante 30 min y pico de 100 TPS 5 min (§1.2).
- [ ] **[FALTA]** Comando único de reentrenamiento (HU-10) y job de las tres razones para casos `REVIEW` y `BLOCK` (§5e).
- [ ] **(ampliación)** `deploy/kind/cluster.yaml`. Detener Compose antes de levantar kind (§8).
- [ ] Objetivos `kind-deploy`, `smoke`, `drift-demo`.

**Aceptación**

- [ ] T-16 y T-17 en verde (§6).
- [ ] «kind create cluster --config deploy/kind/cluster.yaml…» (§7, Fase 5).
- [ ] Al eliminar un pod de scoring bajo carga, el éxito se mantiene en 99 % o más.
- [ ] `make drift-demo` marca `alert = true`.
- [ ] Etiqueta `fase-5`.

---

## 7. Cierre — portafolio

- [ ] `README.md` ← esquema «**Problema:** un banco digital debe mover dinero sin duplicar ni perder fondos…» (§9), los seis apartados.
- [ ] Completar «Resultados a medir» con mediciones propias; las cifras de §1.2 son objetivos, no resultados.
- [ ] En «Decisiones y concesiones»: Spring Boot 3.5 sin soporte open source y migración a 4.1 como primer cambio para producción (0.1).
- [ ] Atribuciones y licencias de los datasets usados (§5a); PaySim es CC BY-SA 4.0.
- [ ] ADR actualizados con lo resuelto en 0.1.
- [ ] Revisión final de firmas; ambos comandos deben salir vacíos:

```bash
git log --format=%B | grep -iE 'co-authored-by:|generated with|-session:'
grep -rniE 'co-authored-by|generated with' --exclude-dir=.git --exclude-dir=.githooks .
```

- [ ] Revisar también las descripciones de los pull requests.
