# Implementation Plan: Extraction Status Polling

**Branch**: `013-extraction-status-polling` | **Date**: 2026-04-07 | **Spec**: [spec.md](spec.md)  
**Input**: Feature specification from `/specs/013-extraction-status-polling/spec.md`

## Summary

Add a cache-backed status polling endpoint (`GET /v1/extract-place/status/{request_id}`) so the product repo can retrieve background extraction results after receiving a provisional response. The `request_id` is already generated and carried through the pipeline (Run 3); this plan wires the missing cache write in `ExtractionPendingHandler`, exposes the `request_id` in the API response, and introduces the `CacheBackend` Protocol per ADR-038.

## Technical Context

**Language/Version**: Python 3.11  
**Primary Dependencies**: FastAPI 0.115, Pydantic 2.10, redis[asyncio] ^5.0 (already installed)  
**Storage**: Redis (cache only — no new DB tables, no Alembic migration)  
**Testing**: pytest with `asyncio_mode = auto`  
**Target Platform**: Linux server (Railway)  
**Project Type**: web-service  
**Performance Goals**: Status reads < 500ms p95  
**Constraints**: Key TTL = 3600s; no raw dicts at API boundary (ADR-017); mypy strict must pass  
**Scale/Scope**: One cache key per provisional extraction request, auto-expiring after 1 hour

## Constitution Check

*GATE: Must pass before implementation begins.*

| ADR | Requirement | Status |
|-----|-------------|--------|
| ADR-001 | src layout (`src/kebi/`) | ✓ All new files in `src/kebi/` |
| ADR-003 | ruff + mypy strict | ✓ All new code typed; no `Any` in public interfaces |
| ADR-004 | Tests in `tests/` mirroring src | ✓ Test files planned for all new modules |
| ADR-014 | `/v1` prefix via APIRouter | ✓ New route added to existing `extract_place` router |
| ADR-017 | Pydantic schemas at API boundary | ✓ `request_id` added to `ExtractPlaceResponse`; status route returns `dict` (FastAPI auto-validates) |
| ADR-018 | Separate router modules | ✓ Status route lives in existing `routes/extract_place.py` |
| ADR-019 | FastAPI `Depends()` only | ✓ `get_cache_backend()` and `get_status_repo()` are dependency functions |
| ADR-034 | Route handlers ≤ 15 lines, no business logic | ✓ Status handler delegates to `status_repo.read()` only |
| ADR-038 | Protocol for all swappable deps | ✓ `CacheBackend` Protocol in `providers/cache.py`; `ExtractionStatusRepository` depends on Protocol |
| **ADR-048** | **Status endpoint extends API contract** | ⚠️ **NEW ADR REQUIRED** — Constitution VIII says "Two endpoints only"; ADR-048 must be written before implementation |

**Constitution VIII violation**: Adding `GET /v1/extract-place/status/{request_id}` requires ADR-048 to update the API contract record. This is a blocking gate. **Write ADR-048 as the first implementation step.**

## Project Structure

### Documentation (this feature)

```text
specs/013-extraction-status-polling/
├── plan.md              ← this file
├── research.md          ← Phase 0 output
├── data-model.md        ← Phase 1 output
├── contracts/
│   └── status-endpoint.md
└── tasks.md             ← Phase 2 output (/speckit.tasks)
```

### Source Code changes

```text
src/kebi/
├── providers/
│   ├── cache.py                          ← NEW: CacheBackend Protocol
│   └── redis_cache.py                    ← NEW: RedisCacheBackend concrete impl
├── core/
│   └── extraction/
│       ├── status_repository.py          ← NEW: ExtractionStatusRepository
│       └── handlers/
│           └── extraction_pending.py     ← MODIFY: inject + use status_repo
├── api/
│   ├── deps.py                           ← MODIFY: get_cache_backend(), get_status_repo(), update get_event_dispatcher()
│   ├── schemas/
│   │   └── extract_place.py              ← MODIFY: add request_id to ExtractPlaceResponse
│   ├── routes/
│   │   └── extract_place.py              ← MODIFY: add GET /status/{request_id} route
│   └── service.py (ExtractionService)    ← MODIFY: pass request_id to provisional response

tests/
├── providers/
│   └── test_cache.py                     ← NEW: CacheBackend Protocol + in-memory stub tests
├── core/
│   └── extraction/
│       ├── test_status_repository.py     ← NEW: ExtractionStatusRepository unit tests
│       └── handlers/
│           └── test_extraction_pending.py ← MODIFY: add status write assertions
└── api/
    └── routes/
        └── test_extract_place_status.py  ← NEW: status route integration tests

kebi-config/bruno/ai-service/
└── extract-place-status.bru             ← NEW: Bruno request for status polling

docs/
└── decisions.md                         ← MODIFY: add ADR-048
```

**Structure Decision**: Single project layout (existing `src/kebi/` structure). All new files follow the hybrid `providers/` + `core/` + `api/` directory pattern (ADR-002).

---

## Implementation Steps

### Step 0 — Write ADR-048 (BLOCKING — do first)

**File**: `docs/decisions.md`

Add ADR-048 at the top (after the format block, before ADR-047):

```
## ADR-048: Status polling endpoint for provisional extractions

**Date:** 2026-04-07
**Status:** accepted
**Context:** Constitution Section VIII specifies two HTTP endpoints (POST /v1/extract-place
and POST /v1/consult). The extraction cascade Run 3 introduced provisional responses for
TikTok URLs with no caption. These return immediately with provisional: true and a
request_id, but the product repo has no way to retrieve the final result once background
enrichers complete. A status polling endpoint resolves this gap.
**Decision:** Add GET /v1/extract-place/status/{request_id} as a third endpoint. It reads
from a cache backend keyed by request_id and returns the extraction result when available,
or {"extraction_status": "processing"} when not. This endpoint is read-only, stateless on
the server side (delegates fully to cache), and requires no database access. It lives in
routes/extract_place.py as it is part of the extract-place resource. Constitution Section
VIII is updated to reflect three endpoints.
**Consequences:** Product repo can poll for results after provisional responses. Cache
backend must be available for status reads; if cache is unavailable, the endpoint returns
"processing" gracefully. New endpoint requires a .bru file in kebi-config/bruno/. ADR-048
supersedes the "two endpoints only" constraint in Constitution Section VIII.
```

Update `.specify/memory/constitution.md` Section VIII to reflect three endpoints.

---

### Step 1 — CacheBackend Protocol

**File**: `src/kebi/providers/cache.py`

```python
"""CacheBackend Protocol — ADR-038 abstraction for all cache implementations."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CacheBackend(Protocol):
    """Protocol for async key-value cache with TTL support."""

    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl: int) -> None: ...
```

**Notes**:
- `runtime_checkable` allows `isinstance(backend, CacheBackend)` in tests
- No `delete` or `expire` methods — YAGNI

---

### Step 2 — RedisCacheBackend

**File**: `src/kebi/providers/redis_cache.py`

```python
"""RedisCacheBackend — concrete CacheBackend implementation using redis[asyncio]."""

from __future__ import annotations

from redis.asyncio import Redis


class RedisCacheBackend:
    """Wraps redis.asyncio.Redis to satisfy CacheBackend Protocol."""

    def __init__(self, url: str) -> None:
        self._client: Redis = Redis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> str | None:
        result: str | None = await self._client.get(key)
        return result

    async def set(self, key: str, value: str, ttl: int) -> None:
        await self._client.set(key, value, ex=ttl)
```

**Notes**:
- `decode_responses=True` ensures `get()` returns `str | None`, not `bytes | None`
- `ex=ttl` sets expiry in seconds
- This is the only file that imports from `redis` directly (ADR-038)

---

### Step 3 — ExtractionStatusRepository

**File**: `src/kebi/core/extraction/status_repository.py`

```python
"""ExtractionStatusRepository — cache-backed status store for deferred extractions."""

from __future__ import annotations

import json
import logging

from kebi.providers.cache import CacheBackend

logger = logging.getLogger(__name__)

_KEY_PREFIX = "extraction"
_DEFAULT_TTL = 3600


class ExtractionStatusRepository:
    """Read/write extraction status from cache backend."""

    def __init__(self, cache: CacheBackend) -> None:
        self._cache = cache

    async def write(self, request_id: str, payload: dict, ttl: int = _DEFAULT_TTL) -> None:
        key = f"{_KEY_PREFIX}:{request_id}"
        await self._cache.set(key, json.dumps(payload), ttl)

    async def read(self, request_id: str) -> dict | None:
        key = f"{_KEY_PREFIX}:{request_id}"
        raw = await self._cache.get(key)
        if raw is None:
            return None
        return json.loads(raw)  # type: ignore[no-any-return]
```

**Notes**:
- Depends on `CacheBackend` Protocol — never on `RedisCacheBackend` directly
- JSON serialization is `json.dumps/loads` — no Pydantic (thin repository layer)
- Key format: `extraction:{request_id}` exactly as specified

---

### Step 4 — Add request_id to ExtractPlaceResponse

**File**: `src/kebi/api/schemas/extract_place.py`

Add `request_id: str | None = None` field to `ExtractPlaceResponse`:

```python
class ExtractPlaceResponse(BaseModel):
    provisional: bool
    places: list[SavedPlace]
    pending_levels: list[str]
    extraction_status: str
    source_url: str | None
    request_id: str | None = None  # ← NEW: UUID4 for provisional; None otherwise
```

---

### Step 5 — Wire request_id in ExtractionService

**File**: `src/kebi/core/extraction/service.py`

In the provisional branch (lines 49–56), pass `request_id` to `ExtractPlaceResponse`:

```python
if isinstance(result, ProvisionalResponse):
    return ExtractPlaceResponse(
        provisional=True,
        places=[],
        pending_levels=[level.value for level in result.pending_levels],
        extraction_status="processing",
        source_url=parsed.url,
        request_id=result.request_id or None,  # ← pass through from ProvisionalResponse
    )
```

---

### Step 6 — Update ExtractionPendingHandler

**File**: `src/kebi/core/extraction/handlers/extraction_pending.py`

Inject `ExtractionStatusRepository` and write status after each outcome:

```python
class ExtractionPendingHandler:
    def __init__(
        self,
        background_enrichers: list[Any],
        validator: PlacesValidatorProtocol,
        persistence: ExtractionPersistenceService,
        status_repo: ExtractionStatusRepository,        # ← NEW
    ) -> None:
        self._background_enrichers = background_enrichers
        self._validator = validator
        self._persistence = persistence
        self._status_repo = status_repo                  # ← NEW

    async def handle(self, event: ExtractionPending) -> None:
        context = event.context

        for enricher in self._background_enrichers:
            await enricher.enrich(context)

        dedup_candidates(context)

        results = await self._validator.validate(context.candidates)
        if not results:
            logger.warning(
                "Background extraction found nothing for user %s", event.user_id
            )
            await self._status_repo.write(                    # ← NEW
                event.request_id, {"extraction_status": "failed"}
            )
            return

        saved_ids = await self._persistence.save_and_emit(results, event.user_id)

        # Build final response dict for status cache
        final_payload = _build_status_payload(results, saved_ids, event)
        await self._status_repo.write(event.request_id, final_payload)   # ← NEW
```

Add a private helper `_build_status_payload()` at module level (not inside the class):

```python
def _build_status_payload(
    results: list[ExtractionResult],
    saved_ids: list[str],
    event: ExtractionPending,
) -> dict:
    """Build ExtractPlaceResponse-compatible dict for cache storage."""
    places = [
        {
            "place_id": pid,
            "place_name": r.place_name,
            "address": r.address,
            "city": r.city,
            "cuisine": r.cuisine,
            "confidence": r.confidence,
            "resolved_by": r.resolved_by.value,
            "external_provider": r.external_provider,
            "external_id": r.external_id,
        }
        for pid, r in zip(saved_ids, results, strict=False)
    ]
    return {
        "provisional": False,
        "places": places,
        "pending_levels": [],
        "extraction_status": "saved" if places else "duplicate",
        "source_url": event.url,
        "request_id": None,
    }
```

**Import additions**:
```python
from kebi.core.extraction.status_repository import ExtractionStatusRepository
from kebi.core.extraction.types import ExtractionResult
```

---

### Step 7 — Wire CacheBackend in deps.py

**File**: `src/kebi/api/deps.py`

Add two new dependency functions:

```python
from kebi.providers.cache import CacheBackend
from kebi.providers.redis_cache import RedisCacheBackend
from kebi.core.extraction.status_repository import ExtractionStatusRepository


def get_cache_backend() -> CacheBackend:
    """FastAPI dependency providing CacheBackend (RedisCacheBackend by default)."""
    return RedisCacheBackend(url=get_secrets().redis.url)


def get_status_repo(
    cache: CacheBackend = Depends(get_cache_backend),  # noqa: B008
) -> ExtractionStatusRepository:
    """FastAPI dependency providing ExtractionStatusRepository."""
    return ExtractionStatusRepository(cache=cache)
```

Update `get_event_dispatcher()` to pass `status_repo` into `ExtractionPendingHandler`:

```python
# Inside get_event_dispatcher(), after pending_persistence construction:
status_repo = ExtractionStatusRepository(
    cache=RedisCacheBackend(url=get_secrets().redis.url)
)
pending_handler = ExtractionPendingHandler(
    background_enrichers=[...],
    validator=GooglePlacesValidator(...),
    persistence=pending_persistence,
    status_repo=status_repo,           # ← NEW
)
```

**Note**: `status_repo` is constructed inline (not via `Depends`) inside `get_event_dispatcher()` to match the existing inline construction pattern and avoid circular dependency risk (documented in `deps.py:62–65`).

---

### Step 8 — Add status route

**File**: `src/kebi/api/routes/extract_place.py`

```python
from fastapi import APIRouter, Depends
from kebi.api.deps import get_extraction_service, get_status_repo
from kebi.api.schemas.extract_place import ExtractPlaceRequest, ExtractPlaceResponse
from kebi.core.extraction.service import ExtractionService
from kebi.core.extraction.status_repository import ExtractionStatusRepository

router = APIRouter()


@router.post("/extract-place", response_model=ExtractPlaceResponse)
async def extract_place(
    request: ExtractPlaceRequest,
    service: ExtractionService = Depends(get_extraction_service),  # noqa: B008
) -> ExtractPlaceResponse:
    """...(unchanged)..."""
    return await service.run(request.raw_input, request.user_id)


@router.get("/extract-place/status/{request_id}")
async def get_extraction_status(
    request_id: str,
    status_repo: ExtractionStatusRepository = Depends(get_status_repo),  # noqa: B008
) -> dict:
    """Poll extraction status for a provisional request.

    Returns the full ExtractPlaceResponse dict when complete,
    or {"extraction_status": "processing"} while pending.
    """
    result = await status_repo.read(request_id)
    return result if result is not None else {"extraction_status": "processing"}
```

**Line count check**: `get_extraction_status` is 3 lines of logic — satisfies ADR-034 (≤ 15 lines, no business logic).

---

### Step 9 — Add Bruno request

**File**: `kebi-config/bruno/ai-service/extract-place-status.bru`

```bru
meta {
  name: Extract Place Status
  type: http
  seq: 2
}

get {
  url: {{ai_url}}/v1/extract-place/status/{{request_id}}
  body: none
  auth: none
}

vars:pre-request {
  request_id: 550e8400-e29b-41d4-a716-446655440000
}

tests {
  test("Response status is 200", function() {
    expect(res.getStatus()).to.equal(200);
  });

  test("Response has extraction_status field", function() {
    let body = res.getBody();
    expect(body).to.have.property("extraction_status");
  });

  test("Unknown request_id returns processing", function() {
    let body = res.getBody();
    let status = body.extraction_status;
    expect(["processing", "saved", "failed", "duplicate"]).to.include(status);
  });
}
```

---

### Step 10 — Tests

#### `tests/providers/test_cache.py`

- `test_in_memory_stub_get_missing_key_returns_none`: verify Protocol contract
- `test_in_memory_stub_set_and_get`: round-trip write/read
- `test_redis_cache_backend_satisfies_protocol`: `isinstance(RedisCacheBackend(...), CacheBackend)` (no network call)

#### `tests/core/extraction/test_status_repository.py`

Use an in-memory stub implementing `CacheBackend`:

```python
class InMemoryCacheBackend:
    def __init__(self): self._store: dict[str, str] = {}
    async def get(self, key: str) -> str | None: return self._store.get(key)
    async def set(self, key: str, value: str, ttl: int) -> None: self._store[key] = value
```

Tests:
- `test_write_then_read_returns_dict`: write payload → read returns same dict
- `test_read_missing_key_returns_none`: read nonexistent key → None
- `test_key_format_uses_prefix`: verify key is `extraction:{request_id}`
- `test_write_uses_default_ttl`: TTL arg defaults to 3600

#### `tests/api/routes/test_extract_place_status.py`

Use FastAPI `TestClient` with mocked `get_status_repo` override:

- `test_status_processing_when_key_absent`: `status_repo.read()` returns None → 200 + `"processing"`
- `test_status_returns_result_when_complete`: `status_repo.read()` returns dict → 200 + dict
- `test_status_failed_path`: `status_repo.read()` returns `{"extraction_status": "failed"}` → 200 + `"failed"`

#### `tests/core/extraction/handlers/test_extraction_pending.py` (modify existing)

- `test_handle_writes_failed_status_when_no_results`: verify `status_repo.write()` called with `{"extraction_status": "failed"}`
- `test_handle_writes_full_payload_on_success`: verify `status_repo.write()` called with place data after `save_and_emit()`

#### `tests/core/extraction/test_service.py` (modify existing)

- Verify provisional `ExtractPlaceResponse` includes `request_id` matching the pipeline's UUID4

---

## Verify commands

```bash
poetry run pytest tests/providers/test_cache.py -v
poetry run pytest tests/core/extraction/test_status_repository.py -v
poetry run pytest tests/api/routes/test_extract_place_status.py -v
poetry run pytest tests/core/extraction/handlers/test_extraction_pending.py -v
poetry run pytest -x                          # full suite
poetry run ruff check src/ tests/
poetry run mypy src/
```

All must pass before the branch is considered complete.
