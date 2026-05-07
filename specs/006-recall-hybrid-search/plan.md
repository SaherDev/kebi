# Implementation Plan: Recall — Hybrid Place Search

**Branch**: `006-recall-hybrid-search` | **Date**: 2026-03-31 | **Spec**: [spec.md](spec.md)

## Summary

Replace the 501 stub on `POST /v1/recall` with a full hybrid search implementation. The endpoint embeds the user's natural language query using Voyage 4-lite (`input_type="query"`), runs a single CTE query combining pgvector cosine similarity and PostgreSQL full-text search on `place_name` + `cuisine`, merges results via Reciprocal Rank Fusion (k=60), and returns the top N results with a deterministic `match_reason`. On embedding failure, the service falls back to text-only search and still returns HTTP 200.

## Technical Context

**Language/Version**: Python 3.11
**Primary Dependencies**: FastAPI 0.115, SQLAlchemy 2.0 async, pgvector, Pydantic 2.10, voyageai ^0.3, asyncpg
**Storage**: PostgreSQL — existing `places` table + `embeddings` table (no new migration)
**Testing**: pytest with `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed)
**Target Platform**: Linux server (Railway)
**Performance Goals**: <2s p95 for collections up to 1,000 saved places per user
**Constraints**: No new Alembic migration; read-only access to `places` + `embeddings`
**Scale/Scope**: Up to 1,000 saved places per user initially; FTS is query-time (no pre-computed GIN index)

## Constitution Check

*Pre-implementation gate. Re-checked after design.*

| ADR | Constraint | Status |
|---|---|---|
| ADR-001 | `src/kebi/` layout | ✓ All new code under `src/kebi/` |
| ADR-002 | Hybrid directories: `api/`, `core/`, `providers/` | ✓ Route in `api/routes/`, service in `core/recall/`, repo in `db/repositories/` |
| ADR-003 | Ruff + mypy strict | ✓ Verify step runs both before completion |
| ADR-004 | Tests mirror `src/` in `tests/` | ✓ `tests/core/recall/`, `tests/db/repositories/`, `tests/api/routes/` |
| ADR-014 | `/v1/` prefix via `APIRouter` from app.yaml | ✓ Recall router uses existing `router` in `main.py` |
| ADR-017 | Pydantic for all request/response schemas | ✓ `RecallRequest`, `RecallResult`, `RecallResponse` |
| ADR-018 | Separate router modules | ✓ `src/kebi/api/routes/recall.py` |
| ADR-019 | FastAPI `Depends()` only | ✓ `get_recall_service()` dep added to `deps.py` |
| ADR-020 | No hardcoded model names | ✓ `get_embedder()` reads from config |
| ADR-025 | Langfuse on every embedding call | ✓ `VoyageEmbedder.embed()` already traces via `get_langfuse_client()` |
| ADR-034 | Route handler = facade (one service call) | ✓ `recall.py` calls `RecallService.run()` only |
| ADR-038 | Protocol abstraction for swappable deps | ✓ `RecallRepository` defined as Protocol |
| ADR-040 | Voyage 4-lite, `input_type="query"` | ✓ Enforced in service; `"document"` never used here |
| Constitution §VIII | "Two endpoints only" note | ⚠ Stale — api-contract.md defines three endpoints. No code violation. |

**Gate result**: PASS. No violations. §VIII note is a documentation artefact; the API contract supersedes it.

**New ADR required**: ADR-045 for hybrid search approach (RRF + pgvector + FTS). Must be added before implementation per constitution governance rule.

## Project Structure

### Documentation (this feature)

```text
specs/006-recall-hybrid-search/
├── plan.md              ← this file
├── research.md          ← Phase 0 complete
├── data-model.md        ← Phase 1 complete
├── contracts/
│   └── recall.md        ← Phase 1 complete
└── tasks.md             ← /speckit.tasks output (not yet created)
```

### Source Code Changes

```text
src/kebi/
├── api/
│   ├── routes/
│   │   └── recall.py          NEW — route handler (facade)
│   ├── schemas/
│   │   └── recall.py          NEW — RecallRequest, RecallResult, RecallResponse
│   ├── deps.py                MODIFY — add get_recall_service()
│   └── main.py                MODIFY — include recall_router
├── core/
│   ├── config.py              MODIFY — add RecallConfig to AppConfig
│   └── recall/
│       ├── __init__.py        NEW
│       └── service.py         NEW — RecallService
└── db/
    └── repositories/
        ├── __init__.py        MODIFY — export RecallRepository, SQLAlchemyRecallRepository
        └── recall_repository.py  NEW — Protocol + SQLAlchemy CTE implementation

config/
└── app.yaml                   MODIFY — add recall: {max_results, rrf_k, candidate_multiplier}

tests/
├── api/
│   └── routes/
│       └── test_recall.py     NEW
├── core/
│   └── recall/
│       ├── __init__.py        NEW
│       └── test_service.py    NEW
└── db/
    └── repositories/
        └── test_recall_repository.py  NEW

docs/
└── decisions.md               MODIFY — add ADR-045

kebi-config/
└── bruno/
    └── ai-service/
        └── recall.bru         NEW (already created)
```

---

## Implementation Phases

### Phase 1 — ADR + Config + Schemas

**Goal**: Lay the foundation. No business logic yet.

#### Task 1.1 — Add ADR-045 to `docs/decisions.md`

Add entry at the top:

```
## ADR-045: Hybrid search for recall via pgvector + FTS + RRF
Date: 2026-03-31
Status: accepted
Context: The recall endpoint must surface saved places matching a natural language query.
  Pure vector search misses exact keyword matches; pure FTS misses semantic matches.
  Combining both with Reciprocal Rank Fusion (RRF) covers both failure modes.
Decision: Recall search uses a single SQL CTE: one branch for pgvector cosine similarity
  on the embeddings table, one branch for PostgreSQL to_tsvector/plainto_tsquery on
  place_name + COALESCE(cuisine, ''), merged via RRF (k=60). All ranking happens in
  the database. match_reason is derived from boolean flags in the CTE, not from an LLM.
  On embedding failure, the service falls back to text-only search (HTTP 200 preserved).
Consequences: RecallRepository holds raw SQL; changes to search logic require SQL edits
  in one place (recall_repository.py). GIN index deferred — can be added via Alembic
  migration when collections exceed 1,000 places.
```

#### Task 1.2 — Add `recall` section to `config/app.yaml`

```yaml
recall:
  max_results: 10
  rrf_k: 60
  candidate_multiplier: 2
```

#### Task 1.3 — Add `RecallConfig` to `src/kebi/core/config.py`

- Add `RecallConfig(BaseModel)` with fields: `max_results: int`, `rrf_k: int`, `candidate_multiplier: int`
- Add `recall: RecallConfig` field to `AppConfig`

#### Task 1.4 — Create `src/kebi/api/schemas/recall.py`

Three Pydantic models:
- `RecallRequest`: `query: str` (min_length=1), `user_id: str`
- `RecallResult`: `place_id`, `place_name`, `address`, `cuisine: str | None`, `price_range: str | None`, `source_url: str | None`, `saved_at: datetime`, `match_reason: str`
- `RecallResponse`: `results: list[RecallResult]`, `total: int`, `empty_state: bool = False`

---

### Phase 2 — Repository

**Goal**: Implement the hybrid SQL query behind a Protocol.

#### Task 2.1 — Create `src/kebi/db/repositories/recall_repository.py`

**Protocol** `RecallRepository`:
```python
async def hybrid_search(
    self,
    user_id: str,
    query_vector: list[float] | None,
    query_text: str,
    limit: int,
    rrf_k: int,
    candidate_multiplier: int,
) -> list[RecallRow]:  # RecallRow = TypedDict or dataclass, not ORM
    ...

async def count_saved_places(self, user_id: str) -> int:
    ...
```

**`SQLAlchemyRecallRepository`** — concrete implementation:

`hybrid_search` behaviour:
- If `query_vector is not None`: execute the full CTE (vector + text + RRF).
- If `query_vector is None`: execute text-only SQL with `ts_rank` ordering; `match_reason` = `"Matched by name or cuisine (semantic unavailable)"`.

Full CTE structure (pseudocode for implementation reference):
```sql
WITH
vector_results AS (
  SELECT p.id, ROW_NUMBER() OVER (ORDER BY e.vector <=> :query_vector) AS rank
  FROM places p JOIN embeddings e ON e.place_id = p.id
  WHERE p.user_id = :user_id
  ORDER BY e.vector <=> :query_vector
  LIMIT :candidate_limit
),
text_results AS (
  SELECT p.id,
    ROW_NUMBER() OVER (
      ORDER BY ts_rank(
        to_tsvector('english', p.place_name || ' ' || COALESCE(p.cuisine, '')),
        plainto_tsquery('english', :query_text)
      ) DESC
    ) AS rank
  FROM places p
  WHERE p.user_id = :user_id
    AND to_tsvector('english', p.place_name || ' ' || COALESCE(p.cuisine, ''))
        @@ plainto_tsquery('english', :query_text)
),
combined AS (
  SELECT
    COALESCE(vr.id, tr.id) AS id,
    COALESCE(1.0 / (:rrf_k + vr.rank), 0) +
    COALESCE(1.0 / (:rrf_k + tr.rank), 0) AS rrf_score,
    (vr.id IS NOT NULL) AS matched_vector,
    (tr.id IS NOT NULL) AS matched_text
  FROM vector_results vr
  FULL OUTER JOIN text_results tr ON vr.id = tr.id
)
SELECT
  p.id           AS place_id,
  p.place_name,
  p.address,
  p.cuisine,
  p.price_range,
  p.source_url,
  p.created_at   AS saved_at,
  CASE
    WHEN c.matched_vector AND c.matched_text
      THEN 'Matched by name, cuisine, and semantic similarity'
    WHEN c.matched_vector
      THEN 'Matched by semantic similarity'
    ELSE
      'Matched by name or cuisine'
  END AS match_reason
FROM combined c
JOIN places p ON p.id = c.id
ORDER BY c.rrf_score DESC
LIMIT :limit
```

`count_saved_places`: simple `SELECT COUNT(*) FROM places WHERE user_id = :user_id`.

#### Task 2.2 — Export from `src/kebi/db/repositories/__init__.py`

Add `RecallRepository`, `SQLAlchemyRecallRepository` to the module exports.

---

### Phase 3 — Service

**Goal**: Orchestrate embedding + search + response construction.

#### Task 3.1 — Create `src/kebi/core/recall/service.py`

`RecallService.__init__`: takes `embedder: EmbedderProtocol`, `recall_repo: RecallRepository`, `config: RecallConfig`.

`RecallService.run(query: str, user_id: str) -> RecallResponse`:

```
1. count = await recall_repo.count_saved_places(user_id)
2. if count == 0:
     return RecallResponse(results=[], total=0, empty_state=True)
3. embedding = None
   try:
     vectors = await embedder.embed([query], input_type="query")
     embedding = vectors[0]
   except RuntimeError:
     logger.warning("Embedding failed; falling back to text-only recall")
4. rows = await recall_repo.hybrid_search(
     user_id=user_id,
     query_vector=embedding,
     query_text=query,
     limit=config.max_results,
     rrf_k=config.rrf_k,
     candidate_multiplier=config.candidate_multiplier,
   )
5. results = [RecallResult(**row) for row in rows]
6. return RecallResponse(results=results, total=len(results), empty_state=False)
```

---

### Phase 4 — Route + Wiring

**Goal**: Expose the service via HTTP and wire all deps.

#### Task 4.1 — Create `src/kebi/api/routes/recall.py`

```python
router = APIRouter()

@router.post("/recall", response_model=RecallResponse)
async def recall(
    request: RecallRequest,
    service: RecallService = Depends(get_recall_service),
) -> RecallResponse:
    return await service.run(request.query, request.user_id)
```

#### Task 4.2 — Add `get_recall_service` to `src/kebi/api/deps.py`

```python
async def get_recall_service(
    db_session: AsyncSession = Depends(get_session),
    config: AppConfig = Depends(get_config),
) -> RecallService:
    return RecallService(
        embedder=get_embedder(),
        recall_repo=SQLAlchemyRecallRepository(db_session),
        config=config.recall,
    )
```

#### Task 4.3 — Update `src/kebi/api/main.py`

```python
from kebi.api.routes.recall import router as recall_router
router.include_router(recall_router, prefix="")
```

---

### Phase 5 — Tests

**Goal**: Full test coverage for all new modules.

#### Task 5.1 — `tests/api/routes/test_recall.py`

Test cases using `httpx.AsyncClient` + `AsyncMock` for the service:
- `test_recall_returns_results` — happy path with 1 result
- `test_recall_empty_state` — user has no saves → `empty_state: True`
- `test_recall_no_match` — has saves, nothing matches → `empty_state: False`, `results: []`
- `test_recall_400_empty_query` — empty query string → 400
- `test_recall_missing_query` — missing query field → 422

#### Task 5.2 — `tests/core/recall/test_service.py`

Unit tests with `AsyncMock` for embedder and repo:
- `test_run_cold_start` — `count_saved_places` returns 0 → immediate empty state response
- `test_run_with_embedding` — embedder succeeds → `hybrid_search` called with vector
- `test_run_embedding_fallback` — embedder raises `RuntimeError` → `hybrid_search` called with `query_vector=None`
- `test_run_no_match` — `count_saved_places` returns >0, `hybrid_search` returns `[]` → `empty_state=False`, `total=0`, no error
- `test_run_returns_correct_schema` — verify `total == len(results)`

#### Task 5.3 — `tests/db/repositories/test_recall_repository.py`

Integration tests against a live DB (via existing `get_session` fixture pattern):
- `test_count_saved_places_zero` — fresh user → 0
- `test_count_saved_places` — after seeding 3 places → 3
- `test_hybrid_search_with_vector` — seed place + embedding, query by vector → returned
- `test_hybrid_search_text_only` — seed place without checking vector (query_vector=None) → text match returned
- `test_hybrid_search_user_isolation` — two users, querying as user A never returns user B's places

---

### Phase 6 — Verify

Run all checks before marking complete:

```bash
poetry run pytest tests/ -x
poetry run ruff check src/ tests/
poetry run ruff format --check src/ tests/
poetry run mypy src/
```

All must pass with zero errors.

---

## Dependency Order

```
Task 1.1 (ADR)
  → Task 1.2 (config yaml)
    → Task 1.3 (RecallConfig in code)
      → Task 1.4 (schemas)
        → Task 2.1 (repository)
          → Task 2.2 (exports)
            → Task 3.1 (service)
              → Task 4.1 (route)
              → Task 4.2 (dep)
                → Task 4.3 (main.py wiring)
                  → Task 5.1, 5.2, 5.3 (tests, parallelisable)
                    → Phase 6 (verify)
```

## Complexity Tracking

No constitution violations requiring justification.
