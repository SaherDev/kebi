# Implementation Plan: Voyage Embedding Pipeline

**Branch**: `005-voyage-embed-pipeline` | **Date**: 2026-03-30 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `specs/005-voyage-embed-pipeline/spec.md`

## Summary

After a new place is saved to the database via `POST /v1/extract-place`, the system generates a 1024-dimensional semantic embedding using Voyage 4-lite and persists it to the `embeddings` table before returning the response. The embedder is abstracted behind a Protocol so no concrete class is imported in business logic. All orchestration lives in `ExtractionService`. Langfuse traces every embedding call. The database column dimension is already correct (VECTOR(1024)) — no new migration is needed.

---

## Technical Context

**Language/Version**: Python 3.11 (>=3.11,<3.14)
**Primary Dependencies**: FastAPI 0.115, SQLAlchemy 2.0 async, Pydantic 2.10, voyageai ^0.3 (AsyncClient), langfuse ^2.0, pgvector
**Storage**: PostgreSQL via asyncpg — `embeddings` table (VECTOR(1024)), `places` table
**Testing**: pytest with `asyncio_mode = "auto"`, mocked voyageai client and SQLAlchemy session
**Target Platform**: Linux server (Railway), local dev on macOS
**Project Type**: FastAPI web service (AI pipeline component)
**Performance Goals**: Embedding call completes within 2s p95; overall extract-place response unaffected beyond embedding call latency
**Constraints**: No raw SQL in repository layer; mypy --strict must pass; ruff check must pass; all 40+ existing tests must continue to pass

---

## Constitution Check

*GATE: Must pass before implementation begins.*

| Rule | Status | Notes |
|------|--------|-------|
| Repo boundary (AI/ML only, no UI/auth) | ✅ PASS | Embedding is AI/ML logic |
| ADR-001: src layout | ✅ PASS | All new files in `src/kebi/` |
| ADR-002: Hybrid directory | ✅ PASS | `providers/` for EmbedderProtocol+VoyageEmbedder; `db/repositories/` for EmbeddingRepository |
| ADR-003: Ruff + mypy strict | ✅ PASS | Enforced in Verify phase |
| ADR-004: pytest in `tests/` | ✅ PASS | Tests mirror src structure |
| ADR-008: extract-place is sequential async, NOT LangGraph | ✅ PASS | Adding sync embedding call to sequential pipeline |
| ADR-017: Pydantic for all request/response | ✅ PASS | No new API boundary changes |
| ADR-020: Provider abstraction reads app.yaml | ✅ PASS | `get_embedder()` reads `models.embedder` from config |
| ADR-025: Langfuse on every LLM/embedding call | ✅ PASS | VoyageEmbedder wraps call in Langfuse generation span |
| ADR-030: Alembic owns places/embeddings/taste_model migrations | ✅ PASS | VECTOR(1024) migration already exists; no new migration needed |
| ADR-034: Route handlers are facades (one service call) | ✅ PASS | `extract_place.py` is unchanged; all new logic in `ExtractionService` |
| ADR-038: Protocol abstraction for swappable dependencies | ✅ PASS | `EmbedderProtocol` defined; concrete class not imported in service code |
| ADR-040: Voyage 4-lite, 1024-dim | ✅ PASS | `VoyageEmbedder` implements this; model read from config |
| ADR-044: Prompt injection mitigation | ✅ N/A | Embedding pipeline makes no LLM calls with injected content — mitigation applies to Node 6 only |
| Constitution VI: This repo writes `embeddings` | ✅ PASS | EmbeddingRepository writes to embeddings table |

**No violations. Proceed.**

---

## Project Structure

### Documentation (this feature)

```text
specs/005-voyage-embed-pipeline/
├── plan.md              ← this file
├── research.md          ← Phase 0 complete
├── data-model.md        ← Phase 1 complete
├── quickstart.md        ← Phase 1 complete
├── checklists/
│   └── requirements.md
└── tasks.md             ← Phase 2 output (/speckit.tasks — NOT created here)
```

### Source Code Changes

```text
src/kebi/
├── providers/
│   ├── embeddings.py           ← NEW: EmbedderProtocol + VoyageEmbedder + get_embedder()
│   ├── llm.py                  ← unchanged
│   └── tracing.py              ← unchanged
├── db/
│   └── repositories/
│       ├── embedding_repository.py   ← NEW: EmbeddingRepository Protocol + SQLAlchemyEmbeddingRepository
│       ├── place_repository.py       ← unchanged
│       └── __init__.py               ← UPDATE: export new types
├── core/
│   └── extraction/
│       └── service.py          ← UPDATE: accept embedder + embedding_repo, call after save
└── api/
    └── deps.py                 ← UPDATE: inject VoyageEmbedder + EmbeddingRepository

tests/
├── providers/
│   └── test_embeddings.py      ← NEW
└── db/
    └── repositories/
        └── test_embedding_repository.py  ← NEW
; Note: tests/core/extraction/test_service.py already exists — UPDATE it
```

---

## Implementation Tasks

### Task 1: Verify migration state and run `alembic upgrade head`

**What**: Confirm the `embeddings.vector` column is `VECTOR(1024)`. Run `alembic upgrade head` to apply any pending migrations.

**Why**: The VECTOR(1024) migration (`f7cf21e03bc7`) already exists in the chain and is depended upon by the current head (`a1b2c3d4e5f6`). No new migration is needed. Running `alembic upgrade head` is sufficient.

**Files**: No code changes. CLI only.

**Verify**: `\d embeddings` in psql shows `vector | vector(1024)`.

---

### Task 2: Create `src/kebi/providers/embeddings.py`

**What**: Define `EmbedderProtocol`, `VoyageEmbedder`, and `get_embedder()` factory.

**Structure**:
```python
; --- Protocol ---
@runtime_checkable
class EmbedderProtocol(Protocol):
    async def embed(self, texts: list[str], input_type: str) -> list[list[float]]: ...

; --- Implementation ---
class VoyageEmbedder:
    """voyageai AsyncClient wrapper implementing EmbedderProtocol."""
    def __init__(self, model: str, api_key: str | None = None) -> None:
        self._model = model
        self._client = voyageai.AsyncClient(api_key=api_key)

    async def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        ; Build description text, call voyageai, trace with Langfuse, return vectors

; --- Factory ---
def get_embedder() -> EmbedderProtocol:
    ; Read config.models["embedder"], get secrets.providers.voyage.api_key
    ; Return VoyageEmbedder(model=..., api_key=...)
```

**Langfuse tracing pattern**:
```python
lf = get_langfuse_client()
generation = (
    lf.generation(name="voyage_embed", model=self._model, input=texts)
    if lf else None
)
try:
    result = await self._client.embed(texts, model=self._model, input_type=input_type)
    if generation:
        generation.end()
    return result.embeddings
except Exception:
    if generation:
        generation.end(level="ERROR")
    raise
```

**Type annotation note**: `voyageai` is untyped (`# type: ignore[import-untyped]` needed, like pgvector).

**Files**: `src/kebi/providers/embeddings.py` (new)

---

### Task 3: Create `src/kebi/db/repositories/embedding_repository.py`

**What**: Define `EmbeddingRepository` Protocol and `SQLAlchemyEmbeddingRepository` concrete class.

**Structure**:
```python
class EmbeddingRepository(Protocol):
    async def upsert_embedding(
        self, place_id: str, vector: list[float], model_name: str
    ) -> None: ...

class SQLAlchemyEmbeddingRepository:
    def __init__(self, session: AsyncSession) -> None: ...

    async def upsert_embedding(
        self, place_id: str, vector: list[float], model_name: str
    ) -> None:
        ; SELECT existing by place_id
        ; If found: DELETE old row
        ; INSERT new Embedding(id=str(uuid4()), place_id=..., vector=..., model_name=...)
        ; On error: rollback, log, raise RuntimeError
```

**Why delete-then-insert**: Semantically cleaner than in-place mutation for a vector column. Keeps `model_name` fresh if the embedder is swapped. Consistent with how place save works (explicit ORM, no raw SQL).

**Files**: `src/kebi/db/repositories/embedding_repository.py` (new)

---

### Task 4: Update `src/kebi/db/repositories/__init__.py`

**What**: Export `EmbeddingRepository` and `SQLAlchemyEmbeddingRepository`.

**Files**: `src/kebi/db/repositories/__init__.py`

---

### Task 5: Update `src/kebi/core/extraction/service.py`

**What**: Add `embedder` and `embedding_repo` parameters to `ExtractionService.__init__()`. After `place_repo.save(place)` succeeds (step 7 only), build the description text, call `embedder.embed()`, and call `embedding_repo.upsert_embedding()`.

**Key constraint**: Embedding only happens on the **new-place path** (step 7). The dedup early-return (step 6) returns before any DB write, so no embedding is needed there.

**Description text construction**:
```python
parts = [place.place_name]
if place.cuisine:
    parts.append(place.cuisine)
parts.append(place.address)
description = ", ".join(parts)
```

**New steps after current step 7**:
```python
; Step 7: Write new Place to database
await self._place_repo.save(place)

; Step 8: Generate and save embedding
description = _build_description(place)
vectors = await self._embedder.embed([description], input_type="document")
await self._embedding_repo.upsert_embedding(
    place_id=place_id,
    vector=vectors[0],
    model_name=get_config().models["embedder"].model,
)
```

**Files**: `src/kebi/core/extraction/service.py`

---

### Task 6: Update `src/kebi/api/deps.py`

**What**: Inject `VoyageEmbedder` and `SQLAlchemyEmbeddingRepository` into `ExtractionService`.

**Change to `get_extraction_service()`**:
```python
from kebi.providers.embeddings import get_embedder
from kebi.db.repositories import SQLAlchemyEmbeddingRepository

async def get_extraction_service(
    db_session: AsyncSession = Depends(get_session),
    config: AppConfig = Depends(get_config),
) -> ExtractionService:
    return ExtractionService(
        dispatcher=build_dispatcher(),
        places_client=GooglePlacesClient(),
        place_repo=SQLAlchemyPlaceRepository(db_session),
        extraction_config=config.extraction,
        embedder=get_embedder(),
        embedding_repo=SQLAlchemyEmbeddingRepository(db_session),
    )
```

**Files**: `src/kebi/api/deps.py`

---

### Task 7: Write tests

**`tests/providers/test_embeddings.py`** (new):
- `test_voyage_embedder_calls_client_with_correct_args`: Mock `voyageai.AsyncClient.embed`, assert texts and input_type forwarded correctly, assert embeddings returned.
- `test_voyage_embedder_handles_langfuse_none`: When `get_langfuse_client()` returns None, embedding still succeeds without error.
- `test_get_embedder_returns_voyage_embedder`: Factory returns an `EmbedderProtocol` instance.

**`tests/db/repositories/test_embedding_repository.py`** (new):
- `test_upsert_embedding_inserts_new`: No existing row → new Embedding row added.
- `test_upsert_embedding_replaces_existing`: Existing row → old deleted, new inserted with fresh vector.
- `test_upsert_embedding_rolls_back_on_error`: DB error → rollback called, RuntimeError raised.

**`tests/core/extraction/test_service.py`** (update existing):
- `test_run_embeds_on_new_place_save`: After successful new-place save, `embedder.embed` called once with `input_type="document"`, `embedding_repo.upsert_embedding` called with correct `place_id` and `vector`.
- `test_run_does_not_embed_on_dedup`: Dedup path (existing place found) → `embedder.embed` NOT called.
- `test_run_embeds_description_without_cuisine`: When `cuisine=None`, description is `"name, address"` (not `"name, None, address"`).

---

## Verify Checklist

```bash
; 1. Apply migrations
poetry run alembic upgrade head

; 2. Type check
poetry run mypy src/

; 3. Lint
poetry run ruff check src/ tests/

; 4. Tests
poetry run pytest

; 5. Manual: submit a place via Bruno / curl
; 6. Verify embeddings row in psql: \d embeddings + SELECT ... FROM embeddings
; 7. Verify Langfuse trace: voyage_embed generation visible in dashboard
```

All commands must return exit code 0. Zero new mypy errors. All existing tests pass.

---

## Complexity Tracking

No Constitution violations. No complexity table needed.
