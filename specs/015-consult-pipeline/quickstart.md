# Quickstart: Consult Pipeline (015)

## Prerequisites

1. `config/.local.yaml` with `google.api_key`, `openai.api_key`, `database.url`
2. PostgreSQL running with `places` table populated (at least one saved place)
3. `poetry install`

## Run dev server

```bash
docker compose up -d
poetry run uvicorn kebi.api.main:app --reload
```

## Test the consult endpoint (sync, with location)

```bash
curl -X POST http://localhost:8000/v1/consult \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user-123",
    "query": "I want sushi nearby",
    "location": {"lat": 13.7563, "lng": 100.5018}
  }'
```

## Test with a named destination (no device location)

```bash
curl -X POST http://localhost:8000/v1/consult \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user-123",
    "query": "good ramen in Tokyo"
  }'
```

## Test with open-now validation

```bash
curl -X POST http://localhost:8000/v1/consult \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user-123",
    "query": "somewhere open now near me",
    "location": {"lat": 13.7563, "lng": 100.5018}
  }'
```

## Run tests

```bash
poetry run pytest tests/core/consult/ tests/core/places/ -v
poetry run pytest  # full suite
```

## Verify

```bash
poetry run ruff check src/ tests/
poetry run mypy src/
```

## Key files after implementation

| File | What changed |
|------|--------------|
| `core/places/places_client.py` | Moved from extraction/; + discover() + validate() |
| `core/consult/types.py` | New: Candidate, CandidateMapper, mappers |
| `core/consult/service.py` | Full rewrite: 6-step pipeline |
| `core/intent/intent_parser.py` | + 3 ParsedIntent fields + updated prompt |
| `core/ranking/service.py` | Updated rank() signature + distance scoring |
| `api/schemas/consult.py` | stream removed, photos optional |
| `api/schemas/recall.py` | + lat, lng |
| `api/deps.py` | + get_consult_service() with 5 deps |
| `api/routes/consult.py` | stream branch removed |
| `config/app.yaml` | + consult.radius_defaults |
| `core/config.py` | + RadiusDefaultsConfig |
| `core/utils/geo.py` | New: haversine_m() |
| `docs/decisions.md` | + ADR-049, ADR-050 |
