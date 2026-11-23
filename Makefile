SHELL := /bin/bash
PYTHON ?= python3.12
VENV_PYTHON := .venv/bin/python
VENV_BIN := $(CURDIR)/.venv/bin

.PHONY: run install clean test

run:
	@set -e; \
	if [ -f .env ]; then \
		set -a; source ./.env; set +a; \
	fi; \
	export PATH="$(VENV_BIN):$$PATH"; \
	exec "$(VENV_PYTHON)" -m gunicorn runner_service.wsgi:application \
		--workers 1 --threads 8 --bind "127.0.0.1:$${PORT:-8080}"

install:
	@if [ ! -f .venv/pyvenv.cfg ] || ! "$(VENV_PYTHON)" -m pip --version >/dev/null 2>&1; then \
		"$(PYTHON)" -m venv .venv; \
	fi
	"$(VENV_PYTHON)" -m pip install -r requirements.txt

clean:
	rm -rf .venv __pycache__ .ruff_cache .pytest_cache
	find runner_service -type d -name __pycache__ -prune -exec rm -rf {} +

test:
	PATH="$(VENV_BIN):$$PATH" "$(VENV_PYTHON)" manage.py test
