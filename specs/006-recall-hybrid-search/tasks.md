# Tasks: Recall — Hybrid Place Search

**Input**: Design documents from `/specs/006-recall-hybrid-search/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/recall.md

**Tests**: Testing tasks are included in Phase 4 to validate each user story's functionality.

**Organization**: Tasks grouped by implementation phase, with user story labels for test coverage.

## Format: `- [ ] [ID] [P?] [Story] Description with file path`

- **[P]**: Can run in parallel (different files, no blocking dependencies)
- **[Story]**: Which user story validates this task (US1, US2, US3, US4)
- Exact file paths included in descriptions

---

## Phase 1: Setup — Configuration & Schemas

**Purpose**: Establish architectural decisions and request/response contracts

- [ ] T001 Add ADR-045 to `docs/decisions.md` — Hybrid search via pgvector + FTS + RRF
- [ ] T002 [P] Add `recall` config section to `config/app.yaml` with `max_results: 10`, `rrf_k: 60`, `candidate_multiplier: 2`
- [ ] T003 Add `RecallConfig(BaseModel)` to `src/kebi/core/config.py` and extend `AppConfig`
- [ ] T004 [P] Create `src/kebi/api/schemas/recall.py` with `RecallRequest`, `RecallResult`, `RecallResponse` Pydantic models

**Checkpoint**: Configuration locked, request/response contracts defined

---

## Phase 2: Data Access — Repository Layer

**Purpose**: Implement the hybrid SQL search query behind a Protocol abstraction

- [ ] T005 Create `src/kebi/db/repositories/recall_repository.py` with `RecallRepository` Protocol and `SQLAlchemyRecallRepository` implementation
  - Protocol: `hybrid_search()`, `count_saved_places()`
  - CTE query: vector branch + text branch + RRF merge + match_reason derivation
  - Fallback: text-only query when `query_vector is None`
  - Handle embedding failure gracefully (return results, not error)

- [ ] T006 [P] Export `RecallRepository`, `SQLAlchemyRecallRepository` from `src/kebi/db/repositories/__init__.py`

**Checkpoint**: Repository implements hybrid search; text-only fallback in place

---

## Phase 3: Business Logic — Service Layer

**Purpose**: Orchestrate embedding + search + response construction

- [ ] T007 Create `src/kebi/core/recall/service.py` with `RecallService` class
  - `run(query: str, user_id: str) -> RecallResponse`
  - Cold start check: return `empty_state: true` if user has zero saves
  - Embedding step: `try/except RuntimeError`, set `embedding = None` on failure
  - Call `hybrid_search()` with vector or None
  - Construct `RecallResponse` with results, total count, empty_state flag
  - Log embedding failures without raising (graceful degradation)

**Checkpoint**: Service handles all user story flows; embedding fallback transparent to caller

---

## Phase 4: HTTP Layer — Route & Wiring

**Purpose**: Expose recall endpoint and wire all dependencies

- [ ] T008 Create `src/kebi/api/routes/recall.py` with route handler
  - `POST /recall` handler (uses existing `/v1` prefix via router)
  - Validate `RecallRequest` (FastAPI + Pydantic)
  - Return `RecallResponse`
  - Return 400 if query is empty (handled by Pydantic min_length=1)

- [ ] T009 [P] Add `get_recall_service()` to `src/kebi/api/deps.py`
  - Dependency injection: `AsyncSession`, `AppConfig`
  - Wire: `get_embedder()`, `SQLAlchemyRecallRepository`, `RecallService`

- [ ] T010 Update `src/kebi/api/main.py`
  - Import `recall_router` from `api.routes.recall`
  - Include router: `router.include_router(recall_router, prefix="")`

- [ ] T011 Create `kebi-config/bruno/ai-service/recall.bru` request file (already created)

**Checkpoint**: Recall endpoint fully wired and accessible at `/v1/recall`

---

## Phase 5: Testing & Validation

**Purpose**: Verify all user stories work independently and together

### Tests for User Story 1 (P1) — Natural Language Place Recall

- [ ] T012 [P] [US1] Create happy-path route test in `tests/api/routes/test_recall.py`
  - Request: `{"query": "cosy ramen spot", "user_id": "test-user-1"}`
  - Verify: HTTP 200, `results` not empty, each result has `match_reason`
  - Verify: `total == len(results)`

- [ ] T013 [P] [US1] Create service unit test in `tests/core/recall/test_service.py`
  - Mock `embedder.embed()` success
  - Mock `recall_repo.hybrid_search()` returns 1 result
  - Verify: `RecallResponse` has result with correct fields

### Tests for User Story 2 (P2) — Cross-Method Search Resilience

- [ ] T014 [P] [US2] Add test for vector+text match in `tests/db/repositories/test_recall_repository.py`
  - Seed place + embedding
  - Query by semantic + keyword → result returned with `match_reason` indicating both methods

- [ ] T015 [P] [US2] Add test for text-only match
  - Seed place + embedding but query matches keyword only
  - Verify: result returned via text search, `match_reason` reflects that

- [ ] T016 [P] [US2] Add test for vector-only match
  - Seed place + embedding with unique vector
  - Query by semantic meaning (no keyword overlap)
  - Verify: result returned via vector search, `match_reason` reflects that

### Tests for User Story 3 (P3) — Cold Start Empty State

- [ ] T017 [P] [US3] Add cold start test in `tests/api/routes/test_recall.py`
  - User with zero saved places queries anything
  - Verify: HTTP 200, `results: []`, `total: 0`, `empty_state: true`

- [ ] T018 [P] [US3] Add service no-match test in `tests/core/recall/test_service.py`
  - User has saves, `count_saved_places()` returns >0
  - `hybrid_search()` returns `[]` (no match)
  - Verify: `empty_state: false`, `total: 0`, no exception raised

### Tests for User Story 4 (P4) — Configurable Result Limit

- [ ] T019 [P] [US4] Add limit test in `tests/db/repositories/test_recall_repository.py`
  - Seed 20+ places matching query
  - Call `hybrid_search()` with `limit=10`
  - Verify: returns exactly 10 results

- [ ] T020 [P] [US4] Add config override test in `tests/api/routes/test_recall.py` (optional future enhancement)
  - Verify config-driven limit is respected

### Embedding Fallback Tests

- [ ] T021 [P] Add embedding failure test in `tests/core/recall/test_service.py`
  - Mock `embedder.embed()` raises `RuntimeError`
  - Verify: service catches exception, calls `hybrid_search(query_vector=None, ...)`
  - Verify: HTTP 200 returned with text-only results (fallback successful)

**Checkpoint**: All user stories tested independently; no 5xx on embedding failure

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Final verification and codebase quality

- [ ] T022 Run full test suite: `poetry run pytest tests/` (all tests must pass)

- [ ] T023 [P] Run code quality checks:
  - `poetry run ruff check src/ tests/`
  - `poetry run ruff format --check src/ tests/`
  - `poetry run mypy src/`

  All must pass with zero errors.

- [ ] T024 Verify empty request handling:
  - Send `POST /v1/recall` with empty `query: ""`
  - Verify: HTTP 422 (Pydantic validation error)

- [ ] T025 [P] Run endpoint validation via Bruno CLI or manual request:
  - POST to `http://localhost:8000/v1/recall` with test request from `recall.bru`
  - Verify: HTTP 200, response schema matches contract

- [ ] T026 Commit all changes with message: `feat(recall): implement hybrid search endpoint with RRF`

**Checkpoint**: All code quality gates pass; feature complete and verified

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — start immediately
- **Phase 2 (Repository)**: Depends on Phase 1 complete — locks API contract
- **Phase 3 (Service)**: Depends on Phase 2 complete — uses repository
- **Phase 4 (HTTP)**: Depends on Phase 3 complete — uses service
- **Phase 5 (Testing)**: Depends on Phase 4 complete — tests all layers
- **Phase 6 (Polish)**: Depends on Phase 5 complete — validates all

### Within Each Phase

- **Phase 1**: T002 and T004 can run in parallel (config and schemas don't depend on each other)
- **Phase 4**: T009 and T011 can run in parallel (deps.py and bruno request separate concerns)
- **Phase 5**: All tests marked [P] can run in parallel (different test files)
- **Phase 6**: Checks marked [P] can run in parallel

### Critical Path

T001 → T002/T004 (parallel) → T003 → T005 → T006 → T007 → T008/T009 (parallel) → T010 → T012+ (tests, parallel) → T022-026

---

## Parallel Opportunities

### Phase 1 Parallelism

```bash
# Config and schemas can start simultaneously
T002: Add recall config to app.yaml
T004: Create recall schemas
# Then both feed into T003 (RecallConfig in core/config.py)
```

### Phase 4 Parallelism

```bash
# Dependency and route can start after service (Phase 3) completes
T009: Add get_recall_service to deps.py
T011: Create bruno request file
# Both T008 and T010 depend on T009
```

### Phase 5 Parallelism (After Phase 4)

```bash
# All user story tests can run in parallel
[US1] T012, T013 — happy path tests
[US2] T014, T015, T016 — cross-method tests
[US3] T017, T018 — cold start tests
[US4] T019, T020 — limit tests
[Fallback] T021 — embedding failure test
```

### Phase 6 Parallelism

```bash
# Code checks can run concurrently
T023 (ruff check, ruff format, mypy)
T025 (Bruno endpoint validation)
```

---

## Implementation Strategy

### MVP First (Validate Core Functionality)

1. **Complete Phase 1**: Setup configuration and schemas
2. **Complete Phase 2**: Repository with hybrid SQL query
3. **Complete Phase 3**: Service orchestration
4. **Complete Phase 4**: HTTP route + wiring
5. **Stop and validate Phase 5**: Run all tests for all user stories
   - US1 (happy path): tests T012, T013 pass
   - US2 (cross-method): tests T014, T015, T016 pass
   - US3 (cold start): tests T017, T018 pass
   - US4 (limit): tests T019, T020 pass
   - Fallback: test T021 passes
6. **Deploy/Demo**: Feature ready for production

### Incremental Delivery

Since all user stories use the same implementation, they must be built together:

1. **Phases 1-4**: Core feature (all stories enabled)
2. **Phase 5**: Validate all stories independently
3. **Phase 6**: Polish and prepare for release

Deployment strategy:

- Build once, test all user stories
- Release with all functionality enabled
- No incremental roll-out of stories (they're tightly coupled in one endpoint)

### Single Developer (Sequential)

```
T001 → T002 → T004 → T003 → T005 → T006 → T007 → T008 → T009 → T010 → T011
  ↓ (Phase 1 done)
T012-T021 (all tests, run together)
  ↓ (Phase 5 done)
T022-T026 (polish, run together)
```

**Estimated effort**: ~4-6 hours for implementation (Phases 1-4) + ~2-3 hours for tests (Phase 5) + ~1 hour for verification (Phase 6)

---

## Notes

- All [P] tasks can run in parallel if team capacity allows, but follow phase ordering
- Each phase must complete before the next starts
- Commit after Phase 4 completes (feature ends)
- Run tests before committing Phase 5
- No test marking failures = feature not ready
- Avoid: partial implementation without testing; skipping verification step
- All task descriptions include exact file paths for clear ownership
