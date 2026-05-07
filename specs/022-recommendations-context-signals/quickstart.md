# Quickstart: 022-recommendations-context-signals

## Prerequisites

```bash
poetry install
docker compose up -d          # PostgreSQL + Redis
cp .env.example .env          # fill in secrets
```

## Run migrations

```bash
poetry run alembic upgrade head
```

## Start dev server

```bash
poetry run uvicorn kebi.api.main:app --reload
```

## Test the feature

### 1. Consult returns recommendation_id

```bash
curl -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id": "test_user", "message": "cheap dinner nearby", "location": {"lat": 13.7563, "lng": 100.5018}}'
```

Check response contains `recommendation_id` in `data`.

### 2. User context

```bash
curl http://localhost:8000/v1/user/context?user_id=test_user
```

Returns `savedPlacesCount` and `chips`.

### 3. Signal endpoint

```bash
# Accept a recommendation (use recommendation_id from step 1)
curl -X POST http://localhost:8000/v1/signal \
  -H "Content-Type: application/json" \
  -d '{"signal_type": "recommendation_accepted", "user_id": "test_user", "recommendation_id": "<id-from-step-1>", "place_id": "google:ChIJtest"}'

# Should return 202

# Bogus recommendation_id → 404
curl -X POST http://localhost:8000/v1/signal \
  -H "Content-Type: application/json" \
  -d '{"signal_type": "recommendation_accepted", "user_id": "test_user", "recommendation_id": "nonexistent", "place_id": "google:ChIJtest"}'
```

## Verify

```bash
poetry run ruff check src/ tests/
poetry run mypy src/
poetry run pytest
```
