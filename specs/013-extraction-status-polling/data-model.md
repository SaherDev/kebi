# Data Model: Extraction Status Polling

## No new database tables

This feature stores state in cache only. No Alembic migration required.

---

## Cache Entry: ExtractionStatus

**Storage**: Cache backend (currently Redis)  
**Key format**: `extraction:{request_id}` where `request_id` is a UUID4 string  
**Value format**: JSON-serialized dict  
**TTL**: 3600 seconds (1 hour)

### Possible value shapes

**While processing** (key absent):
```
(key does not exist)
```

**On failure** (background enrichers found nothing):
```json
{"extraction_status": "failed"}
```

**On success** (full result written by ExtractionPendingHandler):
```json
{
  "provisional": false,
  "places": [
    {
      "place_id": "...",
      "place_name": "...",
      "address": "...",
      "city": "...",
      "cuisine": "...",
      "confidence": 0.87,
      "resolved_by": "whisper_audio",
      "external_provider": "google_places",
      "external_id": "ChIJ..."
    }
  ],
  "pending_levels": [],
  "extraction_status": "saved",
  "source_url": "https://...",
  "request_id": null
}
```

---

## Updated API Schema: ExtractPlaceResponse

**File**: `src/kebi/api/schemas/extract_place.py`

| Field | Type | Notes |
|-------|------|-------|
| `provisional` | `bool` | Existing |
| `places` | `list[SavedPlace]` | Existing |
| `pending_levels` | `list[str]` | Existing |
| `extraction_status` | `str` | Existing |
| `source_url` | `str \| None` | Existing |
| `request_id` | `str \| None = None` | **New** — UUID4 for provisional; None otherwise |

---

## Entity: CacheBackend (Protocol)

**File**: `src/kebi/providers/cache.py`

| Method | Signature | Notes |
|--------|-----------|-------|
| `get` | `async (key: str) -> str \| None` | Returns None if key missing or expired |
| `set` | `async (key: str, value: str, ttl: int) -> None` | Overwrites if exists |

---

## Entity: ExtractionStatusRepository

**File**: `src/kebi/core/extraction/status_repository.py`

| Method | Signature | Notes |
|--------|-----------|-------|
| `write` | `async (request_id: str, payload: dict, ttl: int = 3600) -> None` | JSON-serializes payload; key = `extraction:{request_id}` |
| `read` | `async (request_id: str) -> dict \| None` | Deserializes JSON; None if key missing |

**Constructor**: `__init__(self, cache: CacheBackend)`

---

## State Transition: ExtractionRequest

```
POST /v1/extract-place
         │
         ▼
  [inline extraction]
         │
    success? ──yes──► ExtractPlaceResponse(provisional=False, places=[...])
         │
         no
         │
         ▼
  request_id = uuid4()
  ExtractionPending dispatched
         │
         ▼
  ExtractPlaceResponse(provisional=True, request_id=<uuid>)
  Cache key: absent (caller sees "processing")

         ── background ──►  enrichers run
                            │
                       success? ──yes──► status_repo.write(request_id, full_result_dict)
                            │            Cache key: full result (caller sees saved places)
                            no
                            │
                            ▼
                       status_repo.write(request_id, {"extraction_status": "failed"})
                       Cache key: failed (caller sees "failed")

         ── TTL expiry (1h) ──► Cache key deleted (caller sees "processing" again)
```
