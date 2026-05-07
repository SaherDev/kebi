# Quickstart: Consult Endpoint (Phase 2)

## Prerequisites

- `config/.local.yaml` with `providers.openai.api_key` and `providers.anthropic.api_key`
- Optional: `providers.langfuse.secret_key` and `providers.langfuse.public_key` for tracing
- `config/app.yaml` has `consult:` section with `max_alternatives: 2`, `placeholder_photo_url`, `response_timeout_seconds`
- Docker Compose services running (PostgreSQL, Redis): `docker compose up -d`

## Install new dependency

```bash
poetry add langfuse
```

## Start dev server

```bash
poetry run uvicorn kebi.api.main:app --reload
```

## Test the endpoint

```bash
curl -X POST http://localhost:8000/v1/consult \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user-123",
    "query": "good ramen near Sukhumvit for a date night",
    "location": {"lat": 13.7563, "lng": 100.5018}
  }'
```

Expected response shape:
- `primary.place_name`: string
- `primary.photos`: non-empty array
- `reasoning_steps`: array of 6 objects with step identifiers in order
- `reasoning_steps[0].summary`: contains parsed intent fields

## Run tests

```bash
poetry run pytest tests/core/intent/ tests/core/consult/ tests/api/test_consult.py -v
```

## Quality gates

```bash
poetry run pytest            ; all tests
poetry run ruff check src/ tests/
poetry run mypy src/
```
