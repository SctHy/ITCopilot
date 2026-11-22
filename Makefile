export IT_COPILOT_BUILD_SHA ?= $(shell git rev-parse HEAD)
.PHONY: up up-oidc down run-api run-worker test lint frontend-check
up:
	docker compose up -d --build
up-oidc:
	IT_COPILOT_AUTH_MODE=oidc docker compose --profile oidc up -d --build
down:
	docker compose --profile oidc down
run-api:
	.venv/bin/python -m it_copilot.cmd.api
run-worker:
	.venv/bin/python -m it_copilot.cmd.worker
test:
	.venv/bin/pytest -q
lint:
	.venv/bin/ruff check src tests
frontend-check:
	cd frontend && npm run check
