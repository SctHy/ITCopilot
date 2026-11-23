export IT_COPILOT_BUILD_SHA ?= $(shell git rev-parse HEAD)
.PHONY: up up-oidc down run-api run-worker test lint frontend-check
up:
	docker compose up -d --build
up-oidc:
	IT_COPILOT_AUTH_MODE=oidc docker compose --profile oidc up -d --build
down:
	docker compose --profile oidc down
# Start these targets in separate terminals for local development.
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

.PHONY: bench bench-concurrency bench-fault bench-sse
bench: bench-concurrency bench-fault bench-sse
bench-concurrency bench-fault bench-sse:
	.venv/bin/python -m it_copilot.cmd.bench $(@:bench-%=%) $(BENCH_ARGS)

EVAL = .venv/bin/python -m it_copilot.cmd.eval
BASELINE ?= evaluation/baseline.json
CANDIDATE ?= evaluation/candidate.json

.PHONY: eval-p0 eval-p1 eval-all eval-quick eval-report eval-diff
eval-p0 eval-p1:
	$(EVAL) $(@:eval-%=%) $(EVAL_ARGS)
eval-all:
	$(EVAL) all --profile full --out "$(CANDIDATE)" $(EVAL_ARGS)
eval-quick:
	$(EVAL) all --profile quick $(EVAL_ARGS)
eval-report:
	$(EVAL) report --in "$(BASELINE)" --md evaluation/report.md --html evaluation/report.html $(EVAL_ARGS)
eval-diff:
	$(EVAL) diff --baseline "$(BASELINE)" --candidate "$(CANDIDATE)" $(EVAL_ARGS)
