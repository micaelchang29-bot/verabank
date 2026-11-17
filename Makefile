# Objetivos de VeraBank por fase (docs/diseno.md §7). Credenciales en .env (ver .env.example).
SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: help up-core down-core stop-core test-core demo-idempotency reconcile check-events rail-sim test-rail test-rail-it chaos-checks chaos-rail ml-env ml-test data eda ml-features ml-train ml-eval-test ml-report

help: ## Lista los objetivos
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

up-core: ## Compose (perfil core), tópicos, transfer-service en SERVER_PORT (8081) y semilla
	@scripts/up-core.sh

stop-core: ## Detiene transfer-service
	@scripts/stop-service.sh

down-core: stop-core ## Detiene transfer-service y el compose (conserva los volúmenes)
	@docker compose --profile core down

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
