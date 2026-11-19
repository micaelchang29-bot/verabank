# Objetivos de VeraBank por fase (docs/diseno.md §7). Credenciales en .env (ver .env.example).
SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: help up-core down-core stop-core test-core demo-idempotency reconcile check-events rail-sim test-rail test-rail-it chaos-checks chaos-rail ml-env ml-test data eda ml-features ml-train ml-eval-test ml-report ml-export ml-fixtures ml-psi-ref psi ml-reasons retrain retrain-check promote rollback drift-demo amlgen db-roles migrate-risk migrate-analytics db-grants verify-audit seed-risk load-country-risk test-db build-risk test-risk replay aml-run aml-eval up down perf-scoring fallback-demo settle reset-data bi-seed feature-parity smoke load-seed load-transfers reconcile-full pg-restart-test bi-init bi-up bi-check bi-url test-bi kind-up kind-deploy kind-smoke kind-chaos kind-down kind-check

help: ## Lista los objetivos
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

up: ## Todo en Compose: core + risk + sim (transfer-service remoto, risk-scoring, risk-stream, rail), migraciones y semillas
	@scripts/up.sh

down: ## Detiene todo el Compose (conserva los volúmenes)
	@VERABANK_API_KEYS="$${VERABANK_API_KEYS:-unused}" RISK_SVC_PASSWORD="$${RISK_SVC_PASSWORD:-unused}" SIM_LOADER_PASSWORD="$${SIM_LOADER_PASSWORD:-unused}" ML_JOB_PASSWORD="$${ML_JOB_PASSWORD:-unused}" docker compose --profile core --profile risk --profile sim --profile bi down

up-core: ## Compose (perfil core), tópicos, transfer-service en SERVER_PORT (8081) y semilla
	@scripts/up-core.sh

stop-core: ## Detiene transfer-service
	@scripts/stop-service.sh

down-core: stop-core ## Detiene transfer-service y el compose (conserva los volúmenes)
	@# down no necesita las contraseñas, pero Compose interpola docker-compose.yml entero y las exige: con valores
	@# ficticios solo aquí, la limpieza funciona aunque .env esté incompleto o el arranque haya fallado.
	@VERABANK_API_KEYS="$${VERABANK_API_KEYS:-unused}" RISK_SVC_PASSWORD="$${RISK_SVC_PASSWORD:-unused}" SIM_LOADER_PASSWORD="$${SIM_LOADER_PASSWORD:-unused}" ML_JOB_PASSWORD="$${ML_JOB_PASSWORD:-unused}" docker compose --profile core --profile risk --profile sim --profile bi down

test-core: ## Compila y prueba transfer-service (JUnit 5 + Testcontainers)
	@cd transfer-service && ./mvnw -B verify

demo-idempotency: ## Dos POST con la misma clave: 201 y luego 200 con Idempotent-Replayed
	@scripts/demo-idempotency.sh

reconcile: ## Conciliación de §7: falla si no devuelve 0 y 0
	@scripts/reconcile.sh

check-events: ## Cada transferencia completada tiene su TransferCompleted en Kafka
	@scripts/check-events.sh

# Rail interbancario simulado (simulator/). Parámetros: ver simulator/README.md.
REJECT ?= 0
DUP ?= 0
DELAY ?= 20:200
SILENT ?= 0
LOST ?= 0

rail-sim: ## Rail simulado en primer plano: make rail-sim REJECT=0.5 DUP=0 DELAY=20:200 SILENT=0 LOST=0
	@cd simulator && exec uv run python -m verabank_sim rail --reject-rate $(REJECT) --dup-rate $(DUP) --delay-ms $(DELAY) --silent-rate $(SILENT) --lost-rate $(LOST)

test-rail: ## Pruebas unitarias del simulador (sin Kafka)
	@cd simulator && uv run pytest

test-rail-it: ## Integración del rail con Kafka real (requiere make up-core)
	@cd simulator && uv run pytest -m integration

# Caos del rail (simulator/README.md). Sus valores por defecto (SILENT=0.02, LOST=0.01, DELAY=20:300)
# difieren de los de rail-sim: solo cuenta un valor dado por el usuario (línea de comandos o entorno).
N ?= 2000
DUP_MODE ?= same-id
CHAOS_SILENT = $(if $(filter command line environment override,$(origin SILENT)),$(SILENT),0.02)
CHAOS_LOST = $(if $(filter command line environment override,$(origin LOST)),$(LOST),0.01)
CHAOS_DELAY = $(if $(filter command line environment override,$(origin DELAY)),$(DELAY),20:300)

chaos-checks: ## Comprobaciones de db/checks/chaos.sql (sagas finales, asientos, p99 de compensación)
	@scripts/chaos-checks.sh

chaos-rail: ## Caos: make chaos-rail REJECT=0.2 DUP=0.1 N=2000 [DUP_MODE= SILENT= LOST= DELAY=]
	@REJECT=$(REJECT) DUP=$(DUP) N=$(N) DUP_MODE=$(DUP_MODE) SILENT=$(CHAOS_SILENT) LOST=$(CHAOS_LOST) \
	  DELAY=$(CHAOS_DELAY) scripts/chaos-rail.sh

# Entorno de ML (ml/README.md).
ml-env: ## Crea o actualiza el entorno Python de ml/ desde uv.lock
	@uv sync --project ml --frozen

ml-test: ## Pruebas de ml/ sin las que necesitan PaySim (-m "not data")
	@uv run --project ml --frozen pytest ml/tests -m "not data"

data: ## Descarga PaySim a data/raw/paysim.csv (idempotente; hash en ml/data.sha256)
	@scripts/data.sh

eda: ## Ejecuta ml/notebooks/01_eda.ipynb y guarda las salidas (requiere make data)
	@uv run --project ml --frozen jupyter nbconvert --to notebook --execute --inplace \
	  --ExecutePreprocessor.timeout=1800 ml/notebooks/01_eda.ipynb

ml-features: ## Features de los conjuntos A y B en data/features/*.parquet (requiere make data)
	@uv run --project ml --frozen python -m verabank_ml features --set A
	@uv run --project ml --frozen python -m verabank_ml features --set B

ml-train: ## Entrena, selecciona y fija umbrales con train y valid (nunca toca test): make ml-train [ARGS=--winner xgb]
	@uv run --project ml --frozen python -m verabank_ml train $(ARGS)

ml-eval-test: ## Evalúa UNA vez el modelo campeón en test; repetirlo exige ARGS=--force-test
	@uv run --project ml --frozen python -m verabank_ml eval-test $(ARGS)

ml-report: ## Ablación B (solo valid) y ml/reports/model-report.md
	@uv run --project ml --frozen python -m verabank_ml report

ml-export: ## ONNX del champion y del challenger, golden.jsonl, thresholds.json y manifest.json (ml/artifacts/)
	@uv run --project ml --frozen python -m verabank_ml export

ml-psi-ref: ## Referencias del PSI: train (psi_reference.json, §5f) y valid (psi_reference_valid.json); hashes en manifest.json; tras cambiar el champion
	@MLFLOW_DISABLE_AGENT_HINT=1 uv run --project ml --frozen python -m verabank_ml psi-reference --partition train
	@MLFLOW_DISABLE_AGENT_HINT=1 uv run --project ml --frozen python -m verabank_ml psi-reference --partition valid

psi: ## PSI de las ventanas completas pendientes en analytics.drift_metrics (rol ml_job): make psi [ARGS="--reference valid --recompute --fail-on-alert"] (por defecto --reference valid, la única que genera alertas; train es informativa)
	@scripts/ml-job.sh psi $(ARGS)

ml-reasons: ## Tres razones por caso REVIEW/BLOCK del modelo en analytics.fraud_reasons (SHAP o coeficiente; rol ml_job) [ARGS=--limit N]
	@scripts/ml-job.sh reasons $(ARGS)

retrain: ## Reentrena la familia del champion con scoring_log ⨝ fraud_labels maduras → alias challenger y ml/artifacts/challenger/ [REASON=manual] [FORCE=1: salta la guarda del reloj]
	@scripts/ml-job.sh retrain --reason $(REASON) $(if $(FORCE),--force)

retrain-check: ## Disparadores del reentrenamiento: alerta de drift vigente, caída de recall > 5 puntos, > 30 días simulados [FORCE=1]
	@scripts/ml-job.sh retrain --check $(if $(FORCE),--force)

promote: ## Promueve el challenger si en sombra baja el costo ≥ 2 % sin empeorar el recall; reexporta (D8)
	@scripts/ml-job.sh promote --confirm

rollback: ## Vuelve al previous_champion y reexporta
	@scripts/ml-job.sh rollback --confirm

drift-demo: ## DESTRUCTIVO (CONFIRM=yes): reset, tanda base 577–624, PSI (alerta solo con valid; train informativo), deriva 625–696 × 3 y veredicto [DEMO_TPS=100] [FORCE=1]
	@CONFIRM=$(CONFIRM) DEMO_TPS=$(DEMO_TPS) FORCE=$(FORCE) scripts/drift-demo.sh

ml-fixtures: ## Escenarios de features con respuesta conocida en contracts/fixtures/feature_scenarios.json (sin datos)
	@uv run --project ml --frozen python -m verabank_ml fixtures

# Base de datos de risk (db/README.md). make up-core ya los ejecuta en este orden.
db-roles: ## Crea los roles risk_svc, sim_loader y ml_job (idempotente)
	@scripts/db-roles.sh

migrate-risk: ## Migraciones de db/migrations/risk con Flyway y el rol verabank_risk
	@scripts/db-migrate-risk.sh

migrate-analytics: ## Migraciones de db/migrations/analytics (vista, drift_metrics, fraud_reasons, audit.verify_chain), rol de arranque
	@scripts/db-migrate-analytics.sh

FROM ?= 1

db-grants: ## Permisos de risk_svc, sim_loader, ml_job y BI sobre risk, audit y analytics (idempotente)
	@scripts/db-grants.sh

verify-audit: ## T-16: verifica la cadena de hash de audit.audit_log; sale con 1 si está alterada [FROM=1]
	@FROM=$(FROM) scripts/verify-audit.sh

seed-risk: ## Exime la cuenta CLEARING de las reglas AML (idempotente)
	@scripts/seed-risk.sh

load-country-risk: ## Carga db/seed/country_risk.csv (idempotente; cierra la lista anterior): make load-country-risk [CSV=ruta] [VALID_FROM=2026-01-01 (retroactiva)]
	@VALID_FROM=$(VALID_FROM) scripts/load-country-risk.sh $(CSV)

test-db: ## Pruebas de db/ sobre un PostgreSQL desechable (necesita Docker)
	@uv run --project db --frozen pytest db/tests

# risk-service (Quarkus, roles scoring y stream). Las pruebas usan Testcontainers: necesitan Docker.
test-risk: ## Compila y prueba risk-service (JUnit 5 + Testcontainers PostgreSQL y Kafka)
	@cd risk-service && ./mvnw -B -ntp verify

build-risk: ## Imagen verabank/risk-service:dev (contexto: la raíz, por ml/artifacts)
	@docker build -f risk-service/Dockerfile -t verabank/risk-service:dev .

# Cargas del simulador (simulator/README.md): replay de PaySim y evaluación de las reglas AML. Necesitan make up-core,
# risk-service (roles scoring y stream) y, para aml-eval, make amlgen. Leen .env con scripts/sim.sh.
N ?= 20000
TPS ?= 50
AMOUNT_FACTOR ?= 1
DURATION ?= 5m
VALID_FROM ?=
PEAK ?= 0

replay: ## Replay de PaySim (partición de prueba): make replay N=20000 TPS=50 [AMOUNT_FACTOR=1]
	@scripts/sim.sh replay --n $(N) --tps $(TPS) --amount-factor $(AMOUNT_FACTOR)

# Tandas por pasos y liquidación (fase 5, simulator/README.md). Las ventanas de PSI cubren los pasos [24k+1, 24k+24]
# desde sim_epoch: las tandas empiezan en 577, 625, ... Directamente: scripts/sim.sh replay --steps 3 --first-step 577
STEPS ?= 96
BI_TPS ?= 100
SAMPLE ?= 5000
APPROVE ?= 0
SETTLE_TIMEOUT ?= 15m
CONFIRM ?=
DEMO_TPS ?= 100
REASON ?= manual

settle: ## Espera a que no queden PENDING ni PROCESSING (rol BI): make settle [APPROVE=1 aprueba las IN_REVIEW] [SETTLE_TIMEOUT=15m]
	@scripts/sim.sh settle $(if $(filter 1 yes true,$(APPROVE)),--approve-reviews,) --timeout $(SETTLE_TIMEOUT)

reset-data: ## DESTRUCTIVO: docker compose down -v (borra PostgreSQL, Kafka, rail y Metabase); exige CONFIRM=yes
	@CONFIRM=$(CONFIRM) scripts/reset-data.sh

bi-seed: ## Siembra para BI: replay de STEPS (96) pasos desde el 577 a BI_TPS (100) y settle con aprobación de revisiones [FORCE=1: salta la guarda del reloj]
	@scripts/ml-job.sh clock-guard $(if $(FORCE),--force)
	@scripts/sim.sh replay --steps $(STEPS) --first-step 577 --tps $(BI_TPS)
	@scripts/sim.sh settle --approve-reviews --timeout $(SETTLE_TIMEOUT)

feature-parity: ## Compara las features del replay en risk.scoring_log con features_A.parquet por row_id (ADR-13) [SAMPLE=5000]
	@scripts/sim.sh feature-parity --sample $(SAMPLE)

# BI (fase 5, D9): Metabase en el perfil bi, base de aplicación propia y dashboard creado por API (bi/README.md).
bi-init: ## Crea (idempotente) la base metabase y el rol metabase_app con METABASE_DB_PASSWORD de .env
	@db/scripts/create-metabase-db.sh

bi-up: ## Metabase (perfil bi), bi-init y bi/setup_metabase.py: conexión verabank_bi, 7 tarjetas y el dashboard
	@scripts/bi-up.sh

bi-check: ## Consulta cada tarjeta por POST /api/card/:id/query: filas y las 3 primeras
	@scripts/bi-up.sh --check

bi-url: ## URL de Metabase y del dashboard VeraBank — Riesgo
	@scripts/bi-up.sh --url

test-bi: ## Pruebas de bi/ (parser de docs/kpis.sql e idempotencia del script, API simulada)
	@uv run --project bi --frozen pytest bi/tests

# Carga T-17 (fase 5, D10; docs/resultados/fase-5.md). No corre en la CI.
ACCOUNTS ?= 5000
KIND ?= 0

load-seed: ## Cuentas de la carga: ACCOUNTS orígenes con saldo alto y 2.000 destinos internos (asientos desde CLEARING)
	@scripts/sim.sh load-seed --accounts $(ACCOUNTS) --destinations 2000

load-transfers: ## T-17: load-seed, k6 perf/transfers.js (50 TPS) con muestreo, settle, reconcile-full y verify-audit [DURATION=5m|30m] [PEAK=1]
	@DURATION=$(DURATION) PEAK=$(PEAK) ACCOUNTS=$(ACCOUNTS) scripts/load-transfers.sh

reconcile-full: ## Conciliación de §7 + Σ D = Σ C por transferencia + abiertas tras settle + k6 frente a filas loadtest; todo 0
	@scripts/reconcile-full.sh

pg-restart-test: ## 4.G(c): reinicia PostgreSQL con carga baja; errores mientras cae y tiempo hasta normalizar
	@scripts/pg-restart-test.sh

smoke: ## Humo E2E: readiness, INTERNAL y EXTERNAL COMPLETED, idempotencia, MODEL, alerta THRESHOLD, reconcile [KIND=1]
	@KIND=$(KIND) scripts/smoke.sh

fallback-demo: ## Para risk-scoring, replay N=500 (todo FALLBACK) y comprueba que al volver vuelve MODEL
	@scripts/fallback-demo.sh

perf-scoring: ## k6 contra risk-scoring (50 TPS, DURATION=5m; PEAK=1 añade 100 TPS): make perf-scoring [DURATION=30m] [PEAK=1]
	@DURATION=$(DURATION) PEAK=$(PEAK) scripts/perf-scoring.sh

aml-run: ## Envía data/aml/transfers.jsonl por la API y aprueba las revisiones: make aml-run [TPS=50]
	@scripts/sim.sh aml --in ../data/aml/transfers.jsonl --tps $(TPS) --approve-reviews

aml-eval: aml-run ## aml-run y después la evaluación de las reglas contra la verdad de terreno (docs/resultados/aml-eval.md)
	@scripts/sim.sh aml-eval --truth ../data/aml/ground_truth.jsonl --manifest ../data/aml/manifest.json --out ../docs/resultados/aml-eval.md

# Generador AML con verdad de terreno (simulator/amlgen.yaml; salida en data/aml, ignorada por Git).
SEED ?= 42

amlgen: ## Genera data/aml/{transfers,ground_truth}.jsonl y manifest.json: make amlgen [SEED=42]
	@cd simulator && uv run python -m verabank_sim amlgen --seed $(SEED) --out ../data/aml

# kind (ampliación, ADR-13 D12): manifiestos Kustomize en deploy/kind; Compose debe estar detenido (make down).
KIND_VERSION ?= v0.31.0
KIND_CLUSTER ?= verabank

kind-up: ## Crea el clúster kind (deploy/kind/cluster.yaml): transfer-service en localhost:18080 y Metabase en 13000
	@command -v kind >/dev/null || { echo "falta kind $(KIND_VERSION) (manual 0.3)"; exit 2; }
	@kind get clusters 2>/dev/null | grep -qx $(KIND_CLUSTER) || kind create cluster --name $(KIND_CLUSTER) --config deploy/kind/cluster.yaml

kind-deploy: ## Imágenes a kind, Secret desde .env, manifiestos y espera a Ready (aborta si Compose corre)
	@KIND_CLUSTER=$(KIND_CLUSTER) scripts/kind-deploy.sh

kind-smoke: ## make smoke contra el clúster (KIND=1)
	@KIND=1 scripts/smoke.sh

kind-chaos: ## k6 a 20 TPS 3 min contra 18080 y borrado forzado de un pod de risk-scoring al minuto 1; éxito, FALLBACK y tiempo a Ready
	@KIND_CLUSTER=$(KIND_CLUSTER) scripts/kind-chaos.sh

kind-down: ## DESTRUCTIVO (CONFIRM=yes): borra el clúster kind y sus volúmenes
	@CONFIRM=$(CONFIRM) KIND_CLUSTER=$(KIND_CLUSTER) scripts/kind-down.sh

kind-check: ## Comprobación estática de los manifiestos (sin clúster): límites, probes, réplicas, PDB, ≤ 10 GiB, sin credenciales
	@kubectl kustomize --load-restrictor LoadRestrictionsNone deploy/kind/overlays/kind | python3 deploy/kind/check_manifests.py
