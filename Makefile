PYTHON ?= python

.PHONY: test lint demo up down doctor bootstrap-context baseline

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .

demo:
	$(PYTHON) -m app.cli demo --reset --serve

up:
	docker compose up --build

down:
	docker compose down

doctor:
	$(PYTHON) -m app.cli doctor

bootstrap-context:
	$(PYTHON) -m app.cli bootstrap-context

baseline:
	$(PYTHON) -m app.cli baseline
