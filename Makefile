.PHONY: dev test lint format typecheck

dev:
	poetry run uvicorn kebi.api.main:app --reload --port $${AI_PORT:-8000}

test:
	poetry run pytest

lint:
	poetry run ruff check src/ tests/

format:
	poetry run ruff format src/ tests/

typecheck:
	poetry run mypy src/
