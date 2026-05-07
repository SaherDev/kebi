# Implementation Tasks: Voyage Embedding Pipeline

**Feature**: `005-voyage-embed-pipeline`
**Branch**: `005-voyage-embed-pipeline`
**Date**: 2026-03-30
**Spec**: [spec.md](spec.md)
**Plan**: [plan.md](plan.md)

---

## Implementation Strategy

**MVP Scope**: User Story 1 (US1) — embeddings are generated and stored atomically on new place saves, queryable immediately.

**Delivery**: Foundational abstractions (Phase 2) are built first, then wired into the service layer (Phase 3 US1). US2 and US3 are verification-only phases with no new code.

**Parallel Execution**: Within Phase 2, `embeddings.py` and `embedding_repository.py` can be written in parallel (separate files, independent).

**Testing**: No formal test phase is requested. Acceptance is via:
- End-to-end manual test: submit a place via extract-place, verify embedding in DB + Langfuse
- Type check: mypy --strict passes
- Lint: ruff check passes
- Regression: all existing tests pass

---

## Execution Phases & Tasks

### Phase 1: Setup

- [x] T001 Verify migration state and confirm VECTOR(1024) column exists

**Goal**: Ensure the database schema is ready before any embeddings are written.

**Implementation**: Run `poetry run alembic upgrade head` and confirm with `\d embeddings` in psql that the `vector` column is `VECTOR(1024)`.

**Independent Test**: `\d embeddings` output shows `vector | vector(1024)`; exit code 0.

---

### Phase 2: Foundational

These tasks establish the abstractions required by all user stories. All must complete before Phase 3.

#### T002: Create EmbedderProtocol and VoyageEmbedder

- [x] T002 [P] Create `src/kebi/providers/embeddings.py` with EmbedderProtocol and VoyageEmbedder

**Goal**: Define the embedding abstraction and Voyage implementation.

**Implementation**:
1. Define `@runtime_checkable EmbedderProtocol` with `async def embed(texts: list[str], input_type: str) -> list[list[float]]: ...`
2. Implement `VoyageEmbedder` class wrapping `voyageai.AsyncClient`
3. Implement `VoyageEmbedder.embed()` with Langfuse tracing via `get_langfuse_client()` and `generation()` API
4. Define `get_embedder() -> EmbedderProtocol` factory reading from `config.models["embedder"]` and `secrets.providers.voyage.api_key`

**Key constraints**:
- No concrete class imported in business logic (only Protocol referenced)
- Langfuse tracing is optional (graceful degradation when `get_langfuse_client()` returns None)
- `input_type` parameter is passed through to voyageai (supports "document" and "query")

**Independent Test**: `mypy src/kebi/providers/embeddings.py --strict` passes; `ruff check src/kebi/providers/embeddings.py` passes.

---

#### T003: Create EmbeddingRepository implementations

- [x] T003 [P] Create `src/kebi/db/repositories/embedding_repository.py` with EmbeddingRepository Protocol and SQLAlchemyEmbeddingRepository

**Goal**: Define embedding persistence abstraction and async implementation.

**Implementation**:
1. Define `EmbeddingRepository` Protocol with `async def upsert_embedding(place_id: str, vector: list[float], model_name: str) -> None: ...`
2. Implement `SQLAlchemyEmbeddingRepository` class accepting `AsyncSession`
3. Implement `upsert_embedding()`:
   - Query existing row by `place_id`
   - If found: delete old row
   - Insert new `Embedding(id=str(uuid4()), place_id=place_id, vector=vector, model_name=model_name)`
   - On error: rollback, log with context, raise RuntimeError

**Key constraints**:
- Get-then-update pattern (explicit ORM, no raw SQL)
- No duplicate rows per place (upsert key is `place_id`)
- Error handling includes rollback and logging

**Independent Test**: `mypy src/kebi/db/repositories/embedding_repository.py --strict` passes; `ruff check src/kebi/db/repositories/embedding_repository.py` passes.

---

#### T004: Update repositories __init__ to export types

- [x] T004 Update `src/kebi/db/repositories/__init__.py` to export EmbeddingRepository and SQLAlchemyEmbeddingRepository

**Goal**: Make new types importable from the repositories package.

**Implementation**: Add `from kebi.db.repositories.embedding_repository import EmbeddingRepository, SQLAlchemyEmbeddingRepository` and update `__all__`.

**Independent Test**: `python -c "from kebi.db.repositories import EmbeddingRepository, SQLAlchemyEmbeddingRepository"` succeeds (no import error).

---

### Phase 3: User Story 1 (P1) — Place Saved With Embedding Immediately Available

**Story Goal**: Every new place saved via extract-place has an embedding generated and stored synchronously before the response returns.

**Acceptance Criteria**:
1. New place submission → embedding generated via VoyageEmbedder
2. Embedding stored atomically in `embeddings` table
3. Response returns with `place_id` set
4. Embedding row is queryable immediately

**Independent Test**: Submit place via `curl -X POST http://localhost:8000/v1/extract-place -H "Content-Type: application/json" -d '{"raw_input": "Ramen Nagi, Shinjuku", "user_id": "test-user"}' → expect HTTP 200; query embeddings table → confirm 1 row with 1024 dims and matching `place_id`.

---

#### T005: Update ExtractionService to wire in embedder and embedding repository

- [x] T005 [US1] Update `src/kebi/core/extraction/service.py` to accept embedder and embedding_repo, call after place save

**Goal**: Orchestrate embedding generation in the service layer after a new place is written.

**Implementation**:
1. Add `embedder: EmbedderProtocol` and `embedding_repo: EmbeddingRepository` to `ExtractionService.__init__()`
2. After successful `place_repo.save(place)` (step 7 of pipeline), add embedding logic:
   - Build description text: `", ".join([place_name] + ([cuisine] if cuisine else []) + [address])`
   - Call `await self._embedder.embed([description], input_type="document")`
   - Call `await self._embedding_repo.upsert_embedding(place_id, vectors[0], model_name)`
3. Only embed on the new-place path (step 7); dedup early-return (step 6) skips embedding

**Key constraints**:
- No embedding on dedup path (existing place returns early)
- Description text uses available fields: place_name (required), cuisine (optional), address (required)
- Embedding is awaited synchronously before response returns (no background task)
- All orchestration in service layer; route handler unchanged

**Independent Test**: `mypy src/kebi/core/extraction/service.py --strict` passes; unit test (mocked embedder/repo) confirms `embedder.embed()` and `embedding_repo.upsert_embedding()` called with correct args on new-place path, not called on dedup path.

---

#### T006: Update deps.py to inject embedder and embedding_repo

- [x] T006 [US1] Update `src/kebi/api/deps.py` to inject VoyageEmbedder and SQLAlchemyEmbeddingRepository into ExtractionService

**Goal**: Wire concrete implementations into the service via FastAPI dependency injection.

**Implementation**:
1. Import `get_embedder` from `providers.embeddings` and `SQLAlchemyEmbeddingRepository` from `db.repositories`
2. Update `get_extraction_service()` to instantiate and pass both to `ExtractionService()`
3. `embedder=get_embedder()` (factory function)
4. `embedding_repo=SQLAlchemyEmbeddingRepository(db_session)` (passed instance)

**Key constraints**:
- Use `Depends()` only for pre-existing dependencies; factories return concrete instances directly
- No changes to route handlers

**Independent Test**: `mypy src/kebi/api/deps.py --strict` passes; import check: `python -c "from kebi.api.deps import get_extraction_service"` succeeds.

---

#### T007: Run mypy, ruff, and existing tests to confirm no regressions

- [x] T007 [US1] Run type check, lint, and test suite to validate Phase 1–3 implementation

**Goal**: Verify all new code is type-safe, formatted correctly, and no existing tests break.

**Implementation**:
1. `poetry run mypy src/ --strict` — expect 0 new errors
2. `poetry run ruff check src/ tests/` — expect 0 new violations
3. `poetry run pytest` — expect all tests pass, ≥40 existing tests passing

**Independent Test**: All three commands return exit code 0 with no new errors/violations.

---

#### T008: Manual end-to-end test: extract-place creates embedding

- [x] T008 [US1] Test extract-place endpoint creates embedding row with 1024-dim vector in database

**Goal**: Verify the complete flow: place save → embedding generation → database write → response return.

**Implementation**:
1. Start dev server: `poetry run uvicorn kebi.api.main:app --reload`
2. Submit place: `curl -X POST http://localhost:8000/v1/extract-place -H "Content-Type: application/json" -d '{"raw_input": "Ramen Nagi, Shinjuku, Tokyo", "user_id": "test-user-e2e-001"}'`
3. Confirm HTTP 200 response with `place_id` set
4. Query embeddings: `psql -d kebi -c "SELECT id, place_id, model_name, array_length(vector, 1) as dims FROM embeddings WHERE place_id = '<returned_place_id>' LIMIT 1;"`
5. Verify: 1 row, `dims = 1024`, `model_name = 'voyage-4-lite'`

**Independent Test**: All above checks pass; HTTP 200, DB row exists, dims = 1024, model_name matches config.

---

### Phase 4: User Story 2 (P2) — Embedding Provider Swappable Without Code Changes

**Story Goal**: The embedding provider can be swapped in configuration without any code changes to service or route logic.

**Acceptance Criteria**:
- No concrete EmbedderProtocol implementation imported in service code
- No concrete EmbedderProtocol implementation imported in route code
- `get_embedder()` factory is the only place embedder is instantiated

**Independent Test**: Grep for `VoyageEmbedder` in `src/kebi/core/` and `src/kebi/api/` → expect 0 results (concrete class not imported). Verify `get_embedder()` is only place instantiated.

---

#### T009: Verify no concrete embedder imports in business logic

- [x] T009 [US2] Verify EmbedderProtocol abstraction: no VoyageEmbedder imported in service or route code

**Goal**: Enforce the Protocol abstraction per ADR-038.

**Implementation**:
1. Grep across `src/kebi/core/` and `src/kebi/api/`: `grep -r "VoyageEmbedder" src/kebi/core/ src/kebi/api/` → expect empty result
2. Grep for imports: `grep -r "from.*providers.embeddings import" src/kebi/` → expect only `from kebi.providers.embeddings import get_embedder` (factory, not class)
3. Confirm only `get_embedder()` is imported, never `VoyageEmbedder`

**Independent Test**: Both grep commands return empty results (no concrete imports).

---

### Phase 5: User Story 3 (P3) — Embedding Calls Visible in Observability Dashboard

**Story Goal**: Every embedding call produces a trace in Langfuse, capturing model, input, and duration.

**Acceptance Criteria**:
- Each embedding call wraps voyageai invocation in `lf.generation()` span
- Span includes model name, input texts, and duration
- Failures are traced with error level
- Traces are queryable in Langfuse dashboard

**Independent Test**: Submit place via extract-place endpoint; query Langfuse dashboard for generation named `voyage_embed` with `model="voyage-4-lite"` and input matching the constructed description text.

---

#### T010: Verify Langfuse traces appear for embedding calls

- [x] T010 [US3] Verify Langfuse tracing: every embedding call produces a generation span in Langfuse dashboard

**Goal**: Confirm observability integration per ADR-025.

**Implementation**:
1. Start dev server and ensure `LANGFUSE_*` env vars are set (API key, secret, host)
2. Submit place: `curl -X POST http://localhost:8000/v1/extract-place -H "Content-Type: application/json" -d '{"raw_input": "Test place for tracing", "user_id": "test-user-trace-001"}'`
3. Open Langfuse dashboard (typically http://localhost:3000 for local or cloud dashboard URL)
4. Search for generation with name `voyage_embed`
5. Verify: generation exists, `model` field = `voyage-4-lite`, `input` contains the constructed description text, `duration_ms` is populated

**Independent Test**: Langfuse dashboard shows the trace; generation name, model, and input match expectations.

---

## Task Dependencies & Parallel Execution

### Dependency Graph

```
T001 (migration)
  ↓
T002 (embeddings.py) ←→ T003 (embedding_repository.py) [PARALLEL]
  ↓ and ↓
T004 (__init__.py update)
  ↓ and ↓
T005 (ExtractionService update)
  ↓ and ↓
T006 (deps.py update)
  ↓
T007 (type check, lint, tests)
  ↓
T008 (e2e test)
  ↓
T009 (verify abstractions)
  ↓
T010 (verify Langfuse)
```

### Parallel Execution Opportunities

**Within Phase 2 (Foundational)**:
- T002 and T003 can be developed simultaneously (separate files, no dependencies)
- T004 can start as soon as either T002 or T003 completes

**Sequential Required**:
- T001 must complete first (database schema)
- T005, T006 must follow T002, T003, T004 (they depend on the abstractions)
- T007 validates everything before T008
- T009 and T010 are lightweight checks that can follow T008 immediately

### Estimated Execution Order (with parallelism)

1. T001 (5 min)
2. **Parallel**: T002 (20 min) + T003 (20 min)
3. T004 (2 min)
4. T005 (15 min)
5. T006 (5 min)
6. T007 (10 min: mypy, ruff, pytest)
7. T008 (10 min: manual e2e test)
8. **Parallel**: T009 (5 min) + T010 (10 min)

**Total (with parallelism)**: ~90–100 minutes

---

## Acceptance & Definition of Done

✅ **US1 Complete** when:
- T007 (type check, lint, tests) all pass
- T008 (e2e test) confirms embedding created and stored
- Place saved via extract-place endpoint has corresponding 1024-dim embedding row in database

✅ **US2 Complete** when:
- T009 confirms no concrete imports outside providers layer

✅ **US3 Complete** when:
- T010 confirms Langfuse traces appear in dashboard

✅ **Feature Complete** when:
- All three user stories accept their criteria
- mypy --strict passes
- ruff check passes
- All existing tests pass (zero regressions)
- Branch ready to merge into `dev`
