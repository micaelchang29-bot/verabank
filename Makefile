# Objetivos de VeraBank por fase (docs/diseno.md §7). Credenciales en .env (ver .env.example).
SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: help up-core down-core stop-core test-core demo-idempotency reconcile check-events rail-sim test-rail test-rail-it chaos-checks chaos-rail ml-env ml-test data eda ml-features ml-train ml-eval-test ml-report ml-export ml-fixtures amlgen db-roles migrate-risk db-grants seed-risk load-country-risk test-db build-risk test-risk replay aml-run aml-eval up down perf-scoring fallback-demo

help: ## Lista los objetivos
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

up: ## Todo en Compose: core + risk + sim (transfer-service remoto, risk-scoring, risk-stream, rail), migraciones y semillas
	@scripts/up.sh

down: ## Detiene todo el Compose (conserva los volúmenes)
	@VERABANK_API_KEYS="$${VERABANK_API_KEYS:-unused}" RISK_SVC_PASSWORD="$${RISK_SVC_PASSWORD:-unused}" SIM_LOADER_PASSWORD="$${SIM_LOADER_PASSWORD:-unused}" docker compose --profile core --profile risk --profile sim down

up-core: ## Compose (perfil core), tópicos, transfer-service en SERVER_PORT (8081) y semilla
	@scripts/up-core.sh

stop-core: ## Detiene transfer-service
	@scripts/stop-service.sh

down-core: stop-core ## Detiene transfer-service y el compose (conserva los volúmenes)
	@# down no necesita las contraseñas, pero Compose interpola docker-compose.yml entero y las exige: con valores
	@# ficticios solo aquí, la limpieza funciona aunque .env esté incompleto o el arranque haya fallado.
	@VERABANK_API_KEYS="$${VERABANK_API_KEYS:-unused}" RISK_SVC_PASSWORD="$${RISK_SVC_PASSWORD:-unused}" SIM_LOADER_PASSWORD="$${SIM_LOADER_PASSWORD:-unused}" docker compose --profile core --profile risk --profile sim down

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

ml-fixtures: ## Escenarios de features con respuesta conocida en contracts/fixtures/feature_scenarios.json (sin datos)
	@uv run --project ml --frozen python -m verabank_ml fixtures

# Base de datos de risk (db/README.md). make up-core ya los ejecuta en este orden.
db-roles: ## Crea los roles risk_svc y sim_loader (idempotente)
	@scripts/db-roles.sh

migrate-risk: ## Migraciones de db/migrations/risk con Flyway y el rol verabank_risk
	@scripts/db-migrate-risk.sh

db-grants: ## Permisos de risk_svc, sim_loader y BI sobre risk y audit (idempotente)
	@scripts/db-grants.sh

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
