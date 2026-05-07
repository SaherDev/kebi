# Implementation Plan: Schema, Repository, and Code Quality Fixes

**Branch**: `003-fix-schema-repo-quality` | **Date**: 2026-03-25 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/003-fix-schema-repo-quality/spec.md`

## Summary

Fix 9 issues from the implementation report across four layers: (1) replace provider-locked `google_place_id` with a `(external_provider, external_id)` pair via Alembic migration and backfill; (2) introduce `PlaceRepository` Protocol + `SQLAlchemyPlaceRepository` so `ExtractionService` never holds a raw session; (3) add explicit rollback and structured error logging on DB failures; (4) fix consult OpenAPI docs, provider exports, Railway health probe, and embedding dimension docs.

## Technical Context

**Language/Version**: Python 3.11 (>=3.11,<3.14)
**Primary Dependencies**: FastAPI 0.115, SQLAlchemy 2.0 async, Pydantic 2.10, Alembic 1.14, asyncpg, pytest
**Storage**: PostgreSQL via asyncpg + SQLAlchemy async. pgvector for embeddings.
**Testing**: pytest with `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed)
**Target Platform**: Linux server (Railway)
**Project Type**: Web service (FastAPI)
**Performance Goals**: No new performance requirements — this is a correctness and architecture fix
**Constraints**: mypy --strict must pass. ruff check must pass. All 40 existing tests must continue to pass.
**Scale/Scope**: Affects `places` table, extraction pipeline, and consult route

## Constitution Check

| Gate | Status | Notes |
|------|--------|-------|
| ADR-001: src layout | ✅ Pass | All new files under `src/kebi/` |
| ADR-002: Hybrid dirs (api/, core/, providers/, db/) | ✅ Pass | Repository goes in `db/repositories/` |
| ADR-003: ruff + mypy strict | ✅ Pass | All new code typed; `# type: ignore` added for instructor (L2) |
| ADR-004: pytest in tests/ mirroring src/ | ✅ Pass | `tests/db/repositories/test_place_repository.py` |
| ADR-008: extract-place is sequential async (not LangGraph) | ✅ Pass | No change to pipeline structure |
| ADR-017: Pydantic for all request/response schemas | ✅ Pass | `PlacesMatchResult` renamed fields stay Pydantic |
| ADR-019: FastAPI Depends() for session | ✅ Pass | `SQLAlchemyPlaceRepository` injected via Depends |
| ADR-023: HTTP error mapping (400/422/500) | ✅ Pass | Empty provider → 400 (Pydantic validation) |
| ADR-030: Alembic owns migrations for places/embeddings/taste_model | ✅ Pass | Migration lives in `migrations/` |
| ADR-034: Facade pattern on route handlers | ✅ Pass | consult.py still makes one service call |
| ADR-038: Protocol abstraction for all swappable deps | ✅ Pass | `PlaceRepository` Protocol satisfies this for DB access |
| ADR-041: Provider-agnostic place identity | ✅ Pass | This plan implements it |
| Constitution VI: This repo writes places, DB migrations via Alembic | ✅ Pass | Confirmed |

**Constitution Note**: ADR-030 is the binding decision — Alembic owns AI tables in this repo; TypeORM in the product repo manages users and user_settings.

**Complexity Tracking** (ADR-038 requires justification for repository pattern):

| Item | Why Needed | Simpler Alternative Rejected Because |
|------|-----------|-------------------------------------|
| Repository pattern (H1) | ADR-038 explicitly mandates Protocol abstraction for "database repository implementations" | Direct session in service couples infrastructure to business logic, makes testing require a real DB, and violates an accepted ADR |

## Project Structure

### Documentation (this feature)

```text
specs/003-fix-schema-repo-quality/
├── plan.md              ← this file
├── research.md          ← Phase 0 output
├── data-model.md        ← Phase 1 output
└── tasks.md             ← Phase 2 output (/speckit.tasks)
```

### Source Code changes

```text
src/kebi/
├── db/
│   ├── models.py                          [MODIFY] replace google_place_id
│   └── repositories/
│       ├── __init__.py                    [NEW]
│       └── place_repository.py            [NEW] Protocol + SQLAlchemyPlaceRepository
├── core/
│   └── extraction/
│       ├── places_client.py               [MODIFY] PlacesMatchResult fields
│       └── service.py                     [MODIFY] use PlaceRepository, remove raw session
├── api/
│   ├── deps.py                            [MODIFY] wire repository, fix imports
│   └── routes/
│       └── consult.py                     [MODIFY] status_code=200, responses doc
└── providers/
    ├── __init__.py                        [MODIFY] export get_instructor_client
    └── llm.py                             [MODIFY] type: ignore[import-untyped]

migrations/                                [NEW] Alembic init
├── alembic.ini
├── env.py
└── versions/
    └── 001_provider_agnostic_place_identity.py  [NEW]

db/
└── session.py                             [MODIFY] explicit rollback in get_session

docs/
└── api-contract.md                        [MODIFY] embedding dimension 1536 → 1024

railway.toml                               [MODIFY] add healthcheckPath

tests/
└── db/
    ├── __init__.py                        [NEW]
    └── repositories/
        ├── __init__.py                    [NEW]
        └── test_place_repository.py       [NEW]
```

---

## Phase 1: DB Layer (Schema + Migration + Repository + Session)

### 1.1 — Update `db/models.py` (C1)

Replace `google_place_id` column with `external_provider` + `external_id` + UniqueConstraint.

**File**: `src/kebi/db/models.py`

Changes:
- Remove: `google_place_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)`
- Add:
  ```python
  external_provider: Mapped[str] = mapped_column(String, nullable=False)
  external_id: Mapped[str | None] = mapped_column(String, nullable=True)
  ```
- Add `UniqueConstraint` import from `sqlalchemy`
- Add to `Place.__table_args__`:
  ```python
  __table_args__ = (
      UniqueConstraint("external_provider", "external_id", name="uq_places_provider_external"),
  )
  ```

> Note: The ORM-level `UniqueConstraint` does not enforce the `WHERE external_id IS NOT NULL` partial index. The partial index is created via raw SQL in the Alembic migration for correctness. The ORM constraint is kept for documentation / mypy purposes.

### 1.2 — Initialize Alembic + create migration (C1)

**New**: `migrations/` directory with Alembic configuration.

Steps:
1. Run `poetry run alembic init migrations` to scaffold
2. Configure `migrations/env.py` to import `Base` from `kebi.db.base` and use the asyncpg URL from `get_secrets().database.url`
3. Create revision `001_provider_agnostic_place_identity` with the backfill logic (see `data-model.md` Migration Summary)

**Key env.py pattern** for async SQLAlchemy:
```python
from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
import asyncio

def run_migrations_online() -> None:
    connectable = create_async_engine(get_secrets().database.url)
    async def run() -> None:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    asyncio.run(run())
```

### 1.3 — Create `PlaceRepository` Protocol + `SQLAlchemyPlaceRepository` (H1 + H2)

**New file**: `src/kebi/db/repositories/place_repository.py`

```python
from typing import Protocol
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import logging
from kebi.db.models import Place

logger = logging.getLogger(__name__)

MUTABLE_PLACE_FIELDS = (
    "place_name", "address", "cuisine", "price_range",
    "lat", "lng", "source_url", "validated_at", "confidence", "source",
)

class PlaceRepository(Protocol):
    async def get_by_provider(self, provider: str, external_id: str) -> Place | None: ...
    async def save(self, place: Place) -> Place: ...


class SQLAlchemyPlaceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_provider(self, provider: str, external_id: str) -> Place | None:
        return await self._session.scalar(
            select(Place).filter_by(
                external_provider=provider, external_id=external_id
            )
        )

    async def save(self, place: Place) -> Place:
        try:
            existing: Place | None = None
            if place.external_id is not None:
                existing = await self.get_by_provider(
                    place.external_provider, place.external_id
                )
            if existing is not None:
                for field in MUTABLE_PLACE_FIELDS:
                    setattr(existing, field, getattr(place, field))
                await self._session.commit()
                return existing
            self._session.add(place)
            await self._session.commit()
            return place
        except Exception as e:
            await self._session.rollback()
            logger.error(
                "Failed to save place",
                extra={
                    "external_provider": place.external_provider,
                    "external_id": place.external_id,
                    "error": str(e),
                },
            )
            raise RuntimeError(
                f"Failed to save place "
                f"({place.external_provider}/{place.external_id}): {e}"
            ) from e
```

**New file**: `src/kebi/db/repositories/__init__.py`
```python
from kebi.db.repositories.place_repository import (
    PlaceRepository,
    SQLAlchemyPlaceRepository,
)
__all__ = ["PlaceRepository", "SQLAlchemyPlaceRepository"]
```

### 1.4 — Add explicit rollback to `db/session.py` (M1)

**File**: `src/kebi/db/session.py`

Change `get_session()`:
```python
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with _get_session_factory()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
```

> `async with _get_session_factory()()` already calls `session.close()` on exit. The explicit rollback makes the error contract unambiguous: any exception during request handling rolls back before the session is closed.

---

## Phase 2: Service Layer (ExtractionService + PlacesMatchResult)

### 2.1 — Update `PlacesMatchResult` (C1 ripple)

**File**: `src/kebi/core/extraction/places_client.py`

- Rename field `google_place_id: str | None` → `external_id: str | None`
- Add field `external_provider: str = "google"`
- Update `GooglePlacesClient.validate_place()` to set `external_id=...` (was `google_place_id=...`)

### 2.2 — Refactor `ExtractionService` to use `PlaceRepository` (H1)

**File**: `src/kebi/core/extraction/service.py`

- Replace constructor param `db_session: AsyncSession` with `place_repo: PlaceRepository`
- Remove `from sqlalchemy import select` and `from sqlalchemy.ext.asyncio import AsyncSession`
- Update docstring comment on step 6 from `google_place_id` to `(external_provider, external_id)`
- Step 6 dedup:
  ```python
  if places_match.external_id:
      existing = await self._place_repo.get_by_provider(
          places_match.external_provider, places_match.external_id
      )
      if existing:
          return ExtractPlaceResponse(place_id=existing.id, ...)
  ```
- Step 7 write:
  ```python
  place = Place(
      id=place_id,
      ...
      external_provider=places_match.external_provider,
      external_id=places_match.external_id,
      ...
  )
  await self._place_repo.save(place)
  ```
- Remove bare `self._db_session.add(place)` + `await self._db_session.commit()` (moved into repository)

### 2.3 — Update `api/deps.py` (H1 + M2)

**File**: `src/kebi/api/deps.py`

- Remove `from kebi.providers.llm import get_instructor_client` (direct import)
- Add `from kebi.providers import get_instructor_client` (via public API — M2 fix)
- Replace `db_session: AsyncSession` param with `place_repo` wired from `SQLAlchemyPlaceRepository(db_session)`
- Updated wiring:
  ```python
  async def get_extraction_service(
      db_session: AsyncSession = Depends(get_session),
      config: AppConfig = Depends(get_config),
  ) -> ExtractionService:
      return ExtractionService(
          dispatcher=build_dispatcher(),
          places_client=GooglePlacesClient(),
          place_repo=SQLAlchemyPlaceRepository(db_session),
          extraction_config=config.extraction,
      )
  ```

---

## Phase 3: API Layer

### 3.1 — Fix consult route OpenAPI docs (H3)

**File**: `src/kebi/api/routes/consult.py`

Add to `@router.post`:
```python
@router.post(
    "/consult",
    status_code=200,
    responses={
        200: {
            "description": "Synchronous recommendation response (stream=false)",
            "model": SyncConsultResponse,
        },
    },
)
```

Import `SyncConsultResponse` from `kebi.api.schemas.consult`.

> Do NOT set `response_model=SyncConsultResponse` — that would cause a runtime error when the handler returns `StreamingResponse`. The `responses` dict documents the shape in OpenAPI without enforcing serialization.

### 3.2 — Export `get_instructor_client` from providers (M2)

**File**: `src/kebi/providers/__init__.py`

```python
from kebi.providers.llm import get_llm, get_instructor_client

__all__ = ["get_llm", "get_instructor_client"]
```

---

## Phase 4: Config, Tooling, and Docs

### 4.1 — Add healthcheckPath to `railway.toml` (L1)

**File**: `railway.toml`

```toml
[deploy]
startCommand = "poetry run uvicorn kebi.api.main:app --host \"::\" --port $PORT"
healthcheckPath = "/v1/health"
restartPolicyType = "on_failure"
restartPolicyMaxRetries = 3
```

### 4.2 — Fix Pylance type warning (L2)

**File**: `src/kebi/providers/llm.py`

Line 7 — change:
```python
import instructor
```
to:
```python
import instructor  # type: ignore[import-untyped]
```

### 4.3 — Fix embedding dimension in docs (C2)

**File**: `docs/api-contract.md`

Find all occurrences of `1536` in the embeddings/vector section and replace with `1024`.

> pgvector columns are owned entirely by this repo's Alembic migrations — no cross-repo verification needed.

---

## Phase 5: Tests

### 5.1 — New: `tests/db/repositories/test_place_repository.py`

Tests for `SQLAlchemyPlaceRepository`:

| Test | What it verifies |
|------|-----------------|
| `test_save_new_place` | New place is inserted and returned |
| `test_save_existing_place_updates_mutable_fields` | Re-saving same (provider, external_id) updates name/address/etc |
| `test_save_does_not_update_immutable_fields` | id, user_id, external_provider, external_id unchanged on upsert |
| `test_get_by_provider_returns_none_for_unknown` | Returns None when no match |
| `test_get_by_provider_returns_existing` | Returns correct record |
| `test_save_rollback_on_commit_failure` | Exception triggers rollback + RuntimeError with context |
| `test_save_skips_dedup_when_external_id_is_none` | Place with null external_id always inserts (no dedup attempt) |

Use `AsyncMock` for the session; inject as `SQLAlchemyPlaceRepository(mock_session)`.

### 5.2 — Update: `tests/core/extraction/test_service.py` (or existing service tests)

The `ExtractionService` constructor now takes `place_repo: PlaceRepository` instead of `db_session`.  Update all test fixtures in `tests/api/test_extract_place.py` and any other test that constructs `ExtractionService` directly to pass a `MagicMock` or `AsyncMock` implementing `PlaceRepository`.

### 5.3 — Update: `tests/core/extraction/` for PlacesMatchResult rename

Any test that constructs `PlacesMatchResult(google_place_id=...)` must be updated to `PlacesMatchResult(external_id=...)`.

---

## Verify Commands

Run after each phase before committing:

```bash
poetry run pytest                          # all tests must pass
poetry run ruff check src/ tests/          # zero violations
poetry run ruff format --check src/ tests/ # zero formatting issues
poetry run mypy src/                       # zero type errors
```

Final migration verify (requires running DB):
```bash
poetry run alembic upgrade head            # migration applies cleanly
poetry run alembic downgrade -1            # downgrade works
poetry run alembic upgrade head            # re-apply
```

---

## Commit Plan

One commit per phase:

```
refactor(db): add external_provider/external_id, Alembic migration for places
refactor(db): add PlaceRepository Protocol + SQLAlchemyPlaceRepository
fix(db): explicit rollback in get_session on exception
refactor(extraction): use PlaceRepository in ExtractionService
fix(api): add status_code and responses docs to consult endpoint
fix(providers): export get_instructor_client from providers __init__
chore(config): add healthcheckPath to railway.toml
fix(providers): add type ignore for instructor import
fix(docs): update embedding dimension from 1536 to 1024 in api-contract.md
test(db): add PlaceRepository unit tests
```
