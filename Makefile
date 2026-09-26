.PHONY: env install up down migrate serve test test-integration lint typecheck check data verify-data demo-b data-v2 verify-data-v2 bench-data train demo-c topics demo-pipeline bench-e2e mlflow compare demo-registry setup dashboard dashboard-reset dashboard-build dashboard-user dashboard-test test-services-down smoke docs-check

env:            ## create .env from the example (local development only)
	@test -f .env || cp .env.example .env

install:        ## create .venv and install dependencies from uv.lock
	uv sync

up: env         ## start local services and wait for health
	docker compose up -d --wait postgres redis kafka

down:           ## stop services (keeps data volumes)
	docker compose down

migrate:        ## apply database migrations
	uv run alembic upgrade head

serve:          ## run the scoring API on 127.0.0.1:8100
	uv run uvicorn fraudplat.api.app:create_app --factory --host 127.0.0.1 --port 8100

test:           ## hermetic unit tests (no services)
	uv run pytest

ITEST = docker compose -p fraud-itest -f docker-compose.test.yml
ITEST_ENV = FRAUD_TEST_DATABASE_URL=postgresql://fraud:itest-only@127.0.0.1:5543/fraud_test \
	FRAUD_TEST_REDIS_URL=redis://127.0.0.1:6480/15 FRAUD_TEST_KAFKA_BOOTSTRAP=127.0.0.1:9394

test-integration: ## integration tests on disposable isolated services (never the app stack)
	$(ITEST) up -d --wait
	$(ITEST_ENV) FRAUD_TEST_REDIS_CONTAINER=$$($(ITEST) ps -q redis) \
	  uv run python scripts/wait_for_services.py --timeout 180
	$(ITEST_ENV) FRAUD_TEST_REDIS_CONTAINER=$$($(ITEST) ps -q redis) uv run pytest -m integration

smoke:          ## browser smoke test: real console + built dashboard on a seeded disposable database (no streaming)
	$(ITEST) up -d --wait postgres
	uv run python scripts/browser_smoke.py --admin-url postgresql://fraud:itest-only@127.0.0.1:5543/postgres

docs-check:     ## documentation links and evidence consistency
	uv run python scripts/check_doc_links.py
	uv run python scripts/check_evidence.py

test-services-down: ## stop and discard the disposable integration-test services
	$(ITEST) down

lint:
	uv run ruff check . && uv run ruff format --check .

typecheck:
	uv run mypy

check: lint typecheck test test-integration

data:           ## generate the immutable small dataset (refuses to overwrite)
	uv run python -m fraudplat.simulator generate --config configs/sim-small-v1.toml

verify-data:    ## re-hash files and regenerate from config to confirm determinism
	uv run python -m fraudplat.simulator verify --config configs/sim-small-v1.toml

demo-b:         ## Checkpoint B demonstration (dataset facts, cutoff split, leak prevented)
	uv run python -m fraudplat.demos.checkpoint_b --dataset data/raw/sim-small-v1 --cutoff-day 45

data-v2:        ## generate the immutable handbook-scale dataset sim-v2
	uv run python -m fraudplat.simulator generate --config configs/sim-v2.toml

verify-data-v2:
	uv run python -m fraudplat.simulator verify --config configs/sim-v2.toml

bench-data:     ## benchmark generation and reconstruction at increasing sizes
	uv run python scripts/benchmark_data_pipeline.py --fractions 0.1 0.25 0.5 1.0 --out reports/benchmarks/data_pipeline.json

train:          ## build pre-test features (cached) and train/select the LR model + policy
	uv run python -m fraudplat.training.train --config configs/train-lr-v1.toml

demo-c:         ## one genuine ML decision through the API, identical retry, conflicting retry
	uv run python -m fraudplat.demos.checkpoint_c --reset

topics:         ## create the live topics (broker auto-creation is disabled)
	uv run python -m fraudplat.pipeline.topics --namespace live

demo-pipeline:  ## continuous scoring demo: deterministic sequence + delayed-consumer burst
	uv run python -m fraudplat.demos.pipeline

bench-e2e:      ## end-to-end HTTP + pipeline benchmark (25/50/100 rps, 60 s each after 10 s warm-up)
	uv run python scripts/benchmark_e2e.py --rates 25 50 100 --duration 60 --warmup 10

mlflow:         ## local MLflow server: metadata in PostgreSQL db `mlflow`, artifacts in ./mlartifacts
	set -a; . ./.env; set +a; MLFLOW_DISABLE_AGENT_HINT=1 uv run mlflow server \
	  --backend-store-uri "$$MLFLOW_BACKEND_STORE_URI" --artifacts-destination ./mlartifacts \
	  --serve-artifacts --host 127.0.0.1 --port $${MLFLOW_PORT:-5050} --workers 1

compare:        ## model comparison (rules / LR / XGBoost) on pre-test backtests; logs to MLflow
	set -a; . ./.env; set +a; MLFLOW_DISABLE_AGENT_HINT=1 uv run python -m fraudplat.training.compare --config configs/compare-v1.toml

demo-registry:  ## register baseline + candidate, promote then roll back via the production alias
	set -a; . ./.env; set +a; uv run python -m fraudplat.demos.registry_release

dashboard-build: ## install and build the React dashboard (dashboard/dist, served by the console)
	cd dashboard && npm ci && npm run build

dashboard-user: ## add a local console account: make dashboard-user NAME=alice ROLE=admin
	uv run python -m fraudplat.console.auth add $(NAME) --role $(or $(ROLE),analyst)

setup:          ## one-time: register the committed active bundle by identity; alias production -> it
	set -a; . ./.env; set +a; uv run python -m fraudplat.release.setup_active

dashboard:      ## start the local dashboard deployment (API :8110, console :8200); needs up, mlflow, setup
	set -a; . ./.env; set +a; uv run python -m fraudplat.console.launch

dashboard-reset: ## DESTRUCTIVE for demo state: retire the dashboard namespace's Redis keys/topics, start fresh
	set -a; . ./.env; set +a; uv run python -m fraudplat.console.launch --reset

dashboard-test: ## dashboard component tests and type check
	cd dashboard && npm run typecheck && npm test
