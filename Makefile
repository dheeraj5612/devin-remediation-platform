PYTHON ?= python3
PY = .venv/bin/python
CASE ?= all

.PHONY: test lint demo up down doctor bootstrap-context baseline

.venv/.ready: requirements.lock
	$(PYTHON) -m venv .venv
	$(PY) -m pip install -r requirements.lock
	touch $@

test: .venv/.ready
	$(PY) -m pytest -q

lint: .venv/.ready
	$(PY) -m ruff check .

demo: .venv/.ready
	$(PY) -m app.demo

up:
	docker compose up --build

down:
	docker compose down

doctor: .venv/.ready
	$(PY) -m app.cli doctor

bootstrap-context: .venv/.ready
	$(PY) -m app.cli bootstrap-context

baseline: .venv/.ready
	$(PY) -m app.cli baseline --build --case $(CASE)
