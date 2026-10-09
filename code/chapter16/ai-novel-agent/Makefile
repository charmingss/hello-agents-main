.PHONY: install infra-up infra-core infra-down migrate api worker test test-external lint typecheck verify verify-external

install:
	uv sync --frozen --group dev

infra-up:
	docker compose up -d postgres redis minio qdrant neo4j temporal

infra-core:
	docker compose up -d postgres temporal

infra-down:
	docker compose down

migrate:
	uv run alembic upgrade head

api:
	uv run uvicorn novel_agent.main:app --reload

worker:
	uv run python -m novel_agent.workflows.worker

test:
	uv run pytest -m "not external" -v

test-external:
	RUN_EXTERNAL_TESTS=1 uv run pytest -m external -v

lint:
	uv run ruff check src tests

typecheck:
	uv run mypy src

verify: lint typecheck test

verify-external: lint typecheck test test-external
