# Quickstart: Taste Profile & Memory Redesign

**Feature**: 021-taste-profile-memory | **Branch**: `021-taste-profile-memory`

## Prerequisites

```bash
poetry install
docker compose up -d   # PostgreSQL + Redis
cp .env.example .env         # fill in secrets
```

## Run migration

```bash
poetry run alembic upgrade head
```

## Run tests

```bash
poetry run pytest tests/core/taste/ -x
poetry run pytest tests/core/events/ -x
```

## Verify

```bash
poetry run ruff check src/ tests/
poetry run mypy src/
poetry run uvicorn kebi.api.main:app --reload
```

## Manual test

1. Save a place via `POST /v1/chat` with a save intent
2. Check `interactions` table: new row with `type='save'`
3. Wait 30s (debounce window)
4. Check `taste_model` table: `signal_counts`, `taste_profile_summary`, `chips` populated
5. Check Langfuse: regen trace with signal_counts input and summary+chips output

## Key files to read first

1. `specs/021-taste-profile-memory/spec.md` — what and why
2. `docs/plans/2026-04-17-taste-profile-memory.md` — how (step by step)
3. `src/kebi/core/taste/schemas.py` — all Pydantic models
4. `src/kebi/core/taste/aggregation.py` — pure aggregation logic
5. `src/kebi/core/taste/service.py` — handle_signal + _run_regen
