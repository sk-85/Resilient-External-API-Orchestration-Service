.PHONY: install mock run test lint up down demo

install:            ## create a venv and install everything
	python3 -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt

mock:               ## start the flaky mock upstream on :9000
	python3 mock_external/server.py

run:                ## start the service on :8000 (auto-reload)
	uvicorn app.main:create_app --factory --reload --port 8000

test:
	pytest

lint:
	ruff check .

up:                 ## service + postgres + mock upstream in Docker
	docker compose up --build

down:
	docker compose down -v

demo:               ## walk through the resilience behaviour (service + mock must be running)
	bash scripts/demo.sh
