# Quickstart: Unified Chat Router (017)

## What Changed

- **Before**: 4 separate endpoints (`/v1/extract-place`, `/v1/consult`, `/v1/recall`, `/v1/chat-assistant`)
- **After**: 1 endpoint (`/v1/chat`) — the system classifies intent and dispatches automatically

---

## Setup

```bash
# Start infrastructure
docker compose up -d

# Run migrations (adds consult_logs table)
poetry run alembic upgrade head

# Start dev server
poetry run uvicorn kebi.api.main:app --reload
```

---

## Send a Request

```bash
# Consult (recommendation)
curl -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id": "user_abc", "message": "cheap dinner nearby", "location": {"lat": 13.75, "lng": 100.5}}'

# Extract place
curl -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id": "user_abc", "message": "https://www.tiktok.com/@foodie/video/123"}'

# Recall
curl -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id": "user_abc", "message": "that ramen place I saved from TikTok"}'

# Assistant
curl -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id": "user_abc", "message": "is tipping expected in Japan?"}'

# Clarification (low-confidence message)
curl -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id": "user_abc", "message": "fuji"}'
```

---

## Expected Responses

Each response includes `type`, `message`, and optionally `data`:

```json
{ "type": "consult", "message": "...", "data": { ... } }
{ "type": "clarification", "message": "Are you looking for a place called Fuji...", "data": null }
{ "type": "error", "message": "Something went wrong, try again", "data": { "detail": "..." } }
```

---

## Verify

```bash
poetry run pytest
poetry run ruff check src/
poetry run mypy src/
```
