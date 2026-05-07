# Tasks: Extraction Status Polling

**Input**: Design documents from `/specs/013-extraction-status-polling/`
**Prerequisites**: plan.md ✓, spec.md ✓, research.md ✓, data-model.md ✓, contracts/ ✓

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)
- Exact file paths are included in all descriptions

---

## Phase 1: Setup (Blocking Gate)

**Purpose**: Satisfy the Constitution gate before any code is written. ADR-048 must exist before implementation begins (see plan.md Constitution Check).

- [x] T001 Write ADR-048 in `docs/decisions.md` (status endpoint extends API contract; supersedes Constitution VIII "two endpoints only")
- [x] T002 Update Section VIII of `.specify/memory/constitution.md` to reflect three endpoints

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Cache abstraction layer that all three user stories depend on. Cannot implement any story until these are complete.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [x] T003 [P] Create `CacheBackend` Protocol in `src/kebi/providers/cache.py` (two async methods: `get(key) → str | None`, `set(key, value, ttl) → None`)
- [x] T004 [P] Create `RedisCacheBackend` in `src/kebi/providers/redis_cache.py` (wraps `redis.asyncio.Redis`; `decode_responses=True`; only file that imports `redis` directly)
- [x] T005 Create `ExtractionStatusRepository` in `src/kebi/core/extraction/status_repository.py` (constructor takes `CacheBackend`; `write(request_id, payload, ttl=3600)` and `read(request_id) → dict | None`; key format `extraction:{request_id}`)

**Checkpoint**: Cache Protocol + concrete implementation + repository all exist. US1 implementation can begin.

---

## Phase 3: User Story 1 — Submit and Poll for Extraction Result (Priority: P1) 🎯 MVP

**Goal**: After receiving a provisional response with `request_id`, the product repo can poll `GET /v1/extract-place/status/{request_id}` to retrieve the final extraction result.

**Independent Test**: Submit a no-caption TikTok URL → receive `provisional: true` + `request_id` in response → poll status endpoint → get `processing` while background runs → get full place data after background completes.

### Implementation for User Story 1

- [x] T006 [US1] Add `request_id: str | None = None` field to `ExtractPlaceResponse` in `src/kebi/api/schemas/extract_place.py`
- [x] T007 [US1] Wire `request_id` into provisional `ExtractPlaceResponse` in `src/kebi/core/extraction/service.py` (pass `result.request_id or None` when `isinstance(result, ProvisionalResponse)`)
- [x] T008 [US1] Inject `ExtractionStatusRepository` into `ExtractionPendingHandler` via constructor in `src/kebi/core/extraction/handlers/extraction_pending.py` (add `status_repo: ExtractionStatusRepository` param; add `_build_status_payload()` module-level helper; call `status_repo.write()` on both success and failure paths)
- [x] T009 [US1] Add `get_cache_backend()` and `get_status_repo()` dependency functions to `src/kebi/api/deps.py`; update `get_event_dispatcher()` to construct `ExtractionStatusRepository` inline and pass it to `ExtractionPendingHandler`
- [x] T010 [US1] Add `GET /v1/extract-place/status/{request_id}` route to `src/kebi/api/routes/extract_place.py` (inject `get_status_repo`; return `result if result is not None else {"extraction_status": "processing"}`)

### Tests for User Story 1

- [x] T011 [P] [US1] Add tests to `tests/core/extraction/handlers/test_extraction_pending.py`: verify `status_repo.write()` called with full payload on success and `{"extraction_status": "failed"}` on no-results path
- [x] T012 [P] [US1] Add tests to `tests/core/extraction/test_service.py`: verify provisional `ExtractPlaceResponse` carries `request_id` matching the UUID4 from pipeline
- [x] T013 [US1] Write `tests/api/routes/test_extract_place_status.py`: test `status_repo.read()` returns None → 200 + `{"extraction_status": "processing"}`; returns result dict → 200 + dict; returns `{"extraction_status": "failed"}` → 200 + failed

**Checkpoint**: US1 fully functional — POST returns `request_id`, background writes to cache, GET returns result. Verifiable via manual test with a no-caption TikTok URL.

---

## Phase 4: User Story 2 — Safe Polling for Unknown or Expired Requests (Priority: P2)

**Goal**: Any `request_id` that is unknown, expired, or malformed returns HTTP 200 + `{"extraction_status": "processing"}` without errors.

**Independent Test**: Call `GET /v1/extract-place/status/nonexistent-id` → 200 + `{"extraction_status": "processing"}`.

### Implementation for User Story 2

US2 behavior is already implemented in T010 (`return result if result is not None else {"extraction_status": "processing"}`). No additional code is required — this phase is test-only.

### Tests for User Story 2

- [x] T014 [P] [US2] Extend `tests/api/routes/test_extract_place_status.py`: test unknown `request_id` (random UUID) → 200 + processing; test path with special characters → 200 + processing; test empty-string-adjacent edge case
- [x] T015 [P] [US2] Add test to `tests/core/extraction/test_status_repository.py`: `read()` on nonexistent key returns `None`; `read()` after `write()` returns the dict

**Checkpoint**: US2 fully verified — any unknown ID returns graceful "processing" response.

---

## Phase 5: User Story 3 — Swappable Cache Backend (Priority: P3)

**Goal**: The cache implementation can be swapped to in-memory (or any other backend) without changing `ExtractionStatusRepository` or route code.

**Independent Test**: Instantiate `ExtractionStatusRepository` with an in-memory stub — all read/write operations pass without modification to the repository.

### Implementation for User Story 3

The Protocol and injection pattern are already in place from Phase 2. No additional production code needed — this phase is test-only plus Protocol compliance verification.

### Tests for User Story 3

- [x] T016 [P] [US3] Write `tests/providers/test_cache.py`: define an `InMemoryCacheBackend` stub; verify `isinstance(stub, CacheBackend)` (runtime_checkable); verify `get(missing)` returns None; verify `set` + `get` round-trip
- [x] T017 [P] [US3] Extend `tests/providers/test_cache.py`: verify `isinstance(RedisCacheBackend(...), CacheBackend)` (no network call — structural check only)
- [x] T018 [US3] Extend `tests/core/extraction/test_status_repository.py`: run `write` + `read` using `InMemoryCacheBackend` stub; verify key format is `extraction:{request_id}`; verify default TTL is 3600

**Checkpoint**: Protocol swap verified — `ExtractionStatusRepository` works with any `CacheBackend` implementation.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: External documentation, API collection, and final quality gates.

- [x] T019 [P] Write `kebi-config/bruno/ai-service/extract-place-status.bru` (GET request with `{{ai_url}}/v1/extract-place/status/{{request_id}}`; tests for 200 status and `extraction_status` field presence)
- [x] T020 Run full test suite and fix any failures: `poetry run pytest -x`
- [x] T021 [P] Run linter and fix violations: `poetry run ruff check src/ tests/`
- [x] T022 [P] Run type checker and fix violations: `poetry run mypy src/`

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1 — BLOCKS all user stories
- **Phase 3 (US1)**: Depends on Phase 2 completion
- **Phase 4 (US2)**: Depends on T010 (status route) from Phase 3
- **Phase 5 (US3)**: Depends on T003/T004/T005 from Phase 2 (Protocol exists)
- **Phase 6 (Polish)**: Depends on all story phases complete

### Within Phase 3 (US1) — Sequential Order

- T006 → T007 (schema before service wiring)
- T006, T007 → T008 (schema + service before handler)
- T003, T004, T005 → T009 (protocols before deps wiring)
- T008, T009 → T010 (handler + deps before route)
- T010 → T013 (route exists before route tests)
- T008 → T011 (handler exists before handler tests)
- T007 → T012 (service wiring before service tests)

### Parallel Opportunities per Phase

**Phase 2**: T003 and T004 are independent files — run in parallel.

**Phase 3**: T011 and T012 can run in parallel after T008 and T007 respectively. T013 depends on T010.

**Phase 4**: T014 and T015 are independent — run in parallel.

**Phase 5**: T016 and T017 are independent — run in parallel. T018 depends on T016.

**Phase 6**: T019, T021, T022 are independent — run in parallel after T020.

---

## Parallel Execution Example: Phase 2

```bash
# T003 and T004 can run simultaneously (different files):
Task: "Create CacheBackend Protocol in src/kebi/providers/cache.py"
Task: "Create RedisCacheBackend in src/kebi/providers/redis_cache.py"
# Then T005 after both complete:
Task: "Create ExtractionStatusRepository in src/kebi/core/extraction/status_repository.py"
```

## Parallel Execution Example: Phase 3

```bash
# After T006 (schema) completes:
Task (T007): "Wire request_id in ExtractionService"   # depends on T006
Task (T008): "Inject status_repo into ExtractionPendingHandler"  # independent of T007

# After T008 and T009 complete:
Task (T010): "Add GET status route"

# After T010 completes, T011 and T012 can run in parallel:
Task (T011): "Handler tests"
Task (T012): "Service tests"
```

---

## Implementation Strategy

### MVP First (User Story 1 only — 10 tasks)

1. Complete Phase 1: Setup (T001–T002)
2. Complete Phase 2: Foundational (T003–T005)
3. Complete Phase 3: US1 (T006–T013)
4. **STOP and VALIDATE**: Manual test — submit no-caption TikTok URL, verify `request_id` returned, poll status endpoint
5. Run `poetry run pytest -x` to confirm no regression

### Incremental Delivery

1. Phases 1–3 → Full polling feature works (MVP)
2. Phase 4 → Unknown ID edge cases explicitly tested
3. Phase 5 → Protocol swap verified (future-proofing confirmed)
4. Phase 6 → Bruno collection, linter, type checker all clean

---

## Notes

- [P] tasks = different files, no dependencies on incomplete tasks
- US1 delivers 90% of user value — Phases 4 and 5 are hardening
- `ExtractionPipeline` already generates `request_id` (Run 3) — no changes needed there
- `ProvisionalResponse.request_id` already exists in `types.py` — T006/T007 wire it to the API boundary only
- `RedisCacheBackend` is the only file in the codebase that imports `redis` directly (ADR-038)
- Commit after each phase or logical group; git comment char is `;`
