# Quickstart: Voyage Embedding Pipeline

**Branch**: `005-voyage-embed-pipeline` | **Date**: 2026-03-30

---

## Prerequisites

1. Services running: `docker compose up -d`
2. `config/.local.yaml` has `providers.voyage.api_key` set
3. Dependencies installed: `poetry install`

---

## Run Migrations

```bash
poetry run alembic upgrade head
```

Confirms `embeddings.vector` is `VECTOR(1024)`. No new migration is needed — it was applied in a previous branch.

---

## Start Dev Server

```bash
poetry run uvicorn kebi.api.main:app --reload
```

---

## Test: Submit a Place (Bruno or curl)

```bash
curl -X POST http://localhost:8000/v1/extract-place \
  -H "Content-Type: application/json" \
  -d '{"raw_input": "Ramen Nagi, Shinjuku, Tokyo", "user_id": "test-user-001"}'
```

**Expected**: HTTP 200 with `place_id` set (not null).

**Verify embedding written**:
```sql
SELECT id, place_id, model_name, array_length(vector::float[], 1) as dims
FROM embeddings
ORDER BY created_at DESC LIMIT 5;
```

Expected: `dims = 1024`, `model_name = 'voyage-4-lite'`.

---

## Run Tests

```bash
poetry run pytest
```

All 40+ existing tests must continue to pass. New tests cover:
- `tests/providers/test_embeddings.py` — VoyageEmbedder (mocked voyageai client)
- `tests/db/repositories/test_embedding_repository.py` — EmbeddingRepository (mocked session)
- `tests/core/extraction/test_service.py` — ExtractionService embedding integration (mocked embedder + repo)

---

## Verify Type Checking

```bash
poetry run mypy src/
```

Must pass with no errors.

---

## Verify Langfuse Trace

After submitting a place, open the Langfuse dashboard. A generation named `voyage_embed` should appear with:
- `model`: `voyage-4-lite`
- `input`: the constructed description text
- `input_type`: `document`
