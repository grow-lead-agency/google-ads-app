# GrowLead fork tooling (GRO-1302, Python Standard v1, adopce vlna 1).
# Upstream faborsky/google-ads-app Makefile nemá. Runtime pro lidi zůstává ./setup.sh + ./run.sh.
#
# Záměrně CHYBÍ `fmt` (ruff format / --fix by přepsal upstream kód a rozbil merge z upstreamu)
# a `type` (pyrefly = vlna 4). `lint` je ve vlně 1 jen syntaktická brána (viz [tool.ruff.lint]).

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help
.PHONY: help setup lint test deps-check docs-check check audit lock

help:
	@printf '%s\n' \
		'setup       uv sync --locked' \
		'lint        ruff check (vlna 1: jen syntaktické chyby)' \
		'test        pytest (offline, fake klient, žádná síť)' \
		'deps-check  requirements*.txt == pyproject.toml + uv lock --check' \
		'docs-check  upstream kontrola CLI <-> README/CLAUDE.md' \
		'check       deps-check + lint + test + docs-check (brána před commitem a "hotovo")' \
		'audit       pip-audit nad uv export --no-dev (+ gitleaks, pokud je nainstalovaný)' \
		'lock        uv lock'

setup:
	uv sync --locked

lint:
	uv run ruff check .

test:
	uv run pytest -q

deps-check:
	uv lock --check
	uv run python .github/scripts/check_requirements_sync.py

docs-check:
	uv run python scripts/check_docs_consistency.py

check: deps-check lint test docs-check

audit:
	uv export --format requirements-txt --no-dev --no-emit-project --quiet -o .tmp-pip-audit.txt && { uv run pip-audit -r .tmp-pip-audit.txt --require-hashes --progress-spinner off; rc=$$?; rm -f .tmp-pip-audit.txt; exit $$rc; }
	@if command -v gitleaks >/dev/null 2>&1; then gitleaks git --redact --verbose; else printf '%s\n' 'gitleaks not installed, skipped'; fi

lock:
	uv lock
