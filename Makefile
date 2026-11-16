# Objetivos de VeraBank por fase (docs/diseno.md §7). Credenciales en .env (ver .env.example).
SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: help up-core down-core stop-core test-core demo-idempotency reconcile check-events rail-sim test-rail test-rail-it

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
