.PHONY: help sync install init-db run rundev worker check clean config-list config-set docker-up docker-down

SHELL := /bin/bash
VENV_DIR := $(or $(VIRTUAL_ENV),.venv)
ADMIN := $(VENV_DIR)/bin/strata-admin
WEB := $(VENV_DIR)/bin/strata-web
PYTHON := $(VENV_DIR)/bin/python
GUNICORN := $(VENV_DIR)/bin/gunicorn
RUFF := $(VENV_DIR)/bin/ruff
TY := $(VENV_DIR)/bin/ty

help:
	@echo "Strata - Reporting System"
	@echo "-------------------------"
	@echo "sync     - Sync dependencies with uv (creates venv if needed)"
	@echo "install  - Alias for sync"
	@echo "init-db  - Create a blank database"
	@echo "run      - Run server with production settings (HOST:PORT)"
	@echo "rundev   - Run server with dev settings (DEV_HOST:DEV_PORT, debug=True)"
	@echo "worker   - Run the schedule worker"
	@echo "config-list  - Show all config settings"
	@echo "config-set KEY=key VAL=value  - Set a config value"
	@echo "check    - Run ruff and ty for code quality"
	@echo "clean    - Remove temporary files and database"
	@echo ""
	@echo "Database: instance/strata.sqlite3 (default)"
	@echo "Set STRATA_DB to override, e.g.:"
	@echo "  export STRATA_DB=/data/strata.sqlite3"

sync:
	@echo "--- Syncing dependencies ---"
	@uv sync --extra dev

install: sync

init-db:
	@echo "--- Creating blank database ---"
	@$(ADMIN) init-db
	@echo "Database created. Run 'make run' to start the server."

run:
	@echo "--- Starting server (production settings) ---"
	@$(WEB)

rundev:
	@echo "--- Starting server (dev settings, debug=True) ---"
	@$(WEB) --dev

worker:
	@echo "--- Starting schedule worker ---"
	@$(PYTHON) -m worker.schedule_worker

config-list:
	@$(ADMIN) config list

config-set:
	@$(ADMIN) config set $(KEY) '$(VAL)'

check:
	@echo "--- Running code quality checks ---"
	@$(RUFF) format src
	@$(RUFF) check src --fix
	@if [ -z "$$VIRTUAL_ENV" ]; then unset VIRTUAL_ENV; fi; $(TY) check src

docker-up:
	@mkdir -p instance
	docker compose up -d

docker-down:
	docker compose down

clean:
	@echo "--- Cleaning up ---"
	@find . -type f -name '*.py[co]' -delete
	@find . -type d -name '__pycache__' -delete
	@rm -f instance/strata.sqlite3
	@rm -rf instance/cache
