# Tasks: Extraction Cascade Run 3

**Input**: Design documents from `/specs/012-extraction-cascade-run3/`
**Branch**: `012-extraction-cascade-run3`
**Prerequisites**: plan.md ✅ spec.md ✅ research.md ✅ data-model.md ✅ contracts/ ✅

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no shared dependencies)
- **[Story]**: Which user story this task belongs to
- No setup phase — existing project, already on branch

---

## Phase 1: Foundational — PlaceSaved Migration (serves US3)

**Purpose**: Migrate `PlaceSaved` from `place_id: str` to `place_ids: list[str]`. Must complete before Phase 2 — the persistence service dispatches `PlaceSaved` with `place_ids`, and the taste service must accept the new shape before any event is dispatched.

**⚠️ CRITICAL**: Verify zero residual `place_id` references before moving to Phase 2:
```bash
grep -r "\.place_id\b\|place_id=event" src/kebi/core/events/ src/kebi/core/taste/ src/kebi/core/events/handlers.py tests/
```

- [ ] T001 [P] Update `PlaceSaved` in `src/kebi/core/events/events.py`: rename field `place_id: str` → `place_ids: list[str]`
- [ ] T002 [P] Add dataclass comment to `ExtractionPending` in `src/kebi/core/extraction/types.py` explaining why it is intentionally not a `DomainEvent` subclass
- [ ] T003 [P] Update `EventHandlers.on_place_saved` in `src/kebi/core/events/handlers.py`: change `place_id=event.place_id` → `place_ids=event.place_ids`
- [ ] T004 [P] Update `TasteModelService.handle_place_saved` in `src/kebi/core/taste/service.py`: change signature to `place_ids: list[str]`, log one `InteractionLog` row per place_id, run ONE `_apply_taste_update()` for the batch (not N separate calls)
- [ ] T005 [P] Create `tests/core/events/test_events.py`: test `PlaceSaved` constructs with `place_ids` list, `event_type` is `"place_saved"`, and `place_metadata` defaults to `{}`
- [ ] T006 [P] Update `tests/core/taste/test_service_integration.py`: replace all `place_id=` references on `PlaceSaved` or `handle_place_saved` with `place_ids=`

**Checkpoint**: `poetry run pytest tests/core/events/ tests/core/taste/ -v` — all pass

---

## Phase 2: US1 — Multiple Places Saved (Priority: P1) 🎯 MVP

**Goal**: Save all validated places from a single extraction in one round-trip; return a list of `SavedPlace` objects in `ExtractPlaceResponse`.

**Independent Test**: `POST /v1/extract-place` with a multi-mention TikTok URL → response has `provisional: false`, `places` contains one entry per validated place, `extraction_status: "saved"`.

- [ ] T007 Add `bulk_upsert_embeddings(records: list[tuple[str, list[float], str]]) -> None` to `EmbeddingRepository` Protocol in `src/kebi/db/repositories/embedding_repository.py`
- [ ] T008 Implement `bulk_upsert_embeddings` in `SQLAlchemyEmbeddingRepository` in `src/kebi/db/repositories/embedding_repository.py` using `sqlalchemy.dialects.postgresql.insert` with `on_conflict_do_update(index_elements=["place_id"])` — NOT a loop of individual upserts; empty `records` list → early return; on error: rollback, log, raise `RuntimeError`
- [ ] T009 [P] [US1] Update `SavedPlace` and `ExtractPlaceResponse` in `src/kebi/api/schemas/extract_place.py`: add `SavedPlace` model; rewrite `ExtractPlaceResponse` with fields `provisional`, `places`, `pending_levels`, `extraction_status`, `source_url`; delete `PlaceExtraction`, old `place_id`/`place`/`confidence`/`requires_confirmation` fields
- [ ] T010 [US1] Create `src/kebi/core/extraction/persistence.py`: implement `ExtractionPersistenceService` with `save_and_emit(results, user_id) -> list[str]` and `_build_description(result) -> str`; dedup guard only when `result.external_id is not None`; `result.address or ""` fallback; dispatch `PlaceSaved` AFTER all DB writes, BEFORE embedding batch; call `bulk_upsert_embeddings` (not individual upserts); catch `Exception` broadly on embedding failure (non-fatal, log warning)
- [ ] T011 [US1] Rewrite `src/kebi/core/extraction/service.py`: `ExtractionService(pipeline, persistence)` — 2 deps replacing 7; `run()` calls `parse_input()`, `pipeline.run()`, branches on `ProvisionalResponse` vs `list[ExtractionResult]`, calls `persistence.save_and_emit()`, builds `places` list via `zip(saved_ids, result)`
- [ ] T012 [P] [US1] Create `tests/core/extraction/test_persistence.py`: test new place write + PlaceSaved dispatch; duplicate skip; all-duplicates → no dispatch; embedding RuntimeError non-fatal; single-place (1 description, 1-element bulk call); multi-place (5 descriptions, one bulk_upsert_embeddings call with 5 records); `bulk_upsert_embeddings([])` no-op; returns saved place ID list
- [ ] T013 [US1] Rewrite `tests/core/extraction/test_service.py`: test list[ExtractionResult] path → `provisional=False`; ProvisionalResponse path → `provisional=True, persistence NOT called`; empty raw_input → ValueError; all-duplicates → `extraction_status="duplicate"`; places length matches saved_ids length
- [ ] T014 [US1] Add factory functions to `src/kebi/api/deps.py`: `get_place_repo`, `get_embedding_repo`, `get_extraction_config`, `get_embedder_dep`, `get_extraction_persistence`, `get_extraction_pipeline` (wires all enrichers, validator, background enrichers), updated `get_extraction_service(pipeline, persistence)`
- [ ] T015 [US1] Update `tests/api/test_extract_place.py`: remove assertions on old fields (`place_id`, `place`, `confidence`, `requires_confirmation`); add assertions on `provisional`, `places`, `extraction_status`, `pending_levels`; mock `get_extraction_service` to return new `ExtractPlaceResponse` shape

**Checkpoint**: `poetry run pytest tests/core/extraction/test_persistence.py tests/core/extraction/test_service.py tests/api/test_extract_place.py -v` — all pass

---

## Phase 3: US2 — Provisional Response + Background Wiring (Priority: P1)

**Goal**: When inline enrichers find nothing, return `provisional: true` immediately and run background enrichers asynchronously via `ExtractionPendingHandler`.

**Independent Test**: `POST /v1/extract-place` with a URL that produces no inline candidates → response has `provisional: true`, `extraction_status: "processing"`, `pending_levels` lists the three background enricher names.

**⚠️ Circular dep guard**: `get_event_dispatcher` MUST NOT take `Depends(get_extraction_persistence)` — construct `ExtractionPersistenceService` inline using the already-injected `db_session`. See plan.md Phase 12 for the correct pattern.

- [ ] T016 [US2] Update `src/kebi/core/extraction/handlers/extraction_pending.py`: replace `persistence: Any` with `persistence: ExtractionPersistenceService`; import from `persistence.py`; remove TODO comment
- [ ] T017 [US2] Update `tests/core/extraction/handlers/test_extraction_pending_handler.py`: replace `MagicMock()` for persistence with `AsyncMock(spec=ExtractionPersistenceService)`
- [ ] T018 [US2] Register `ExtractionPendingHandler` in `get_event_dispatcher()` in `src/kebi/api/deps.py`: construct `ExtractionPersistenceService` directly with `db_session` (NOT via `Depends(get_extraction_persistence)`); wire background enrichers and validator; `dispatcher.register_handler("extraction_pending", handler.handle)`

**Checkpoint**: `poetry run pytest tests/core/extraction/handlers/ tests/api/test_extract_place.py -v` — all pass

---

## Phase 4: Mypy Gate (hard stop before cleanup)

**Purpose**: Catch all type errors introduced by the rewrite at the integration layer before any files are deleted.

- [ ] T019 Run `poetry run mypy src/` and fix ALL type errors — zero errors required before proceeding to Phase 5

---

## Phase 5: US4 — Dead Code Removal (Priority: P3)

**Goal**: Delete the old 9-step linear pipeline; leave one extraction path.

**Independent Test**: `grep -r "ExtractionDispatcher\|InputExtractor\|ExtractionSource\|compute_confidence\|UnsupportedInputError\|PlainTextExtractor\|TikTokExtractor\|ExtractionFailedNoMatchError" src/ tests/` → zero matches.

- [ ] T020 [P] Delete `src/kebi/core/extraction/dispatcher.py`
- [ ] T021 [P] Delete `src/kebi/core/extraction/extractors/tiktok.py`
- [ ] T022 [P] Delete `src/kebi/core/extraction/extractors/plain_text.py`
- [ ] T023 [P] Delete `src/kebi/core/extraction/result.py`
- [ ] T024 Remove `ExtractionSource` enum and `compute_confidence()` function from `src/kebi/core/extraction/confidence.py`; keep `calculate_confidence()` and `ConfidenceConfig`
- [ ] T025 Remove `InputExtractor` Protocol and its import of legacy `ExtractionResult` from `src/kebi/core/extraction/protocols.py`
- [ ] T026 Remove `ExtractionFailedNoMatchError` from `src/kebi/api/errors.py`
- [ ] T027 Update NOTE comment on `ExtractionResult` in `src/kebi/core/extraction/types.py`: remove "coexists with legacy" language
- [ ] T028 [P] [US4] Rewrite "Data Flow: Extract a Place" section in `docs/architecture.md` to describe the three-phase cascade (enrichment → validation → background dispatch)
- [ ] T029 [P] [US4] Update `docs/api-contract.md` extract-place response to the multi-place provisional shape per `specs/012-extraction-cascade-run3/contracts/extract-place-v2.md`

---

## Phase 6: Polish & Final Verification

- [ ] T030 Run grep verification: `grep -r "ExtractionDispatcher\|InputExtractor\|ExtractionSource\|compute_confidence\|UnsupportedInputError\|PlainTextExtractor\|TikTokExtractor\|ExtractionFailedNoMatchError" src/ tests/` — expected zero matches
- [ ] T031 Run full suite: `poetry run pytest && poetry run ruff check src/ tests/ && poetry run mypy src/` — all must pass with zero failures

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Foundational)**: No dependencies — start immediately
- **Phase 2 (US1)**: Requires Phase 1 complete (persistence service dispatches `PlaceSaved` with `place_ids`)
- **Phase 3 (US2)**: Requires Phase 2 complete (`ExtractionPersistenceService` must exist before handler uses it)
- **Phase 4 (Mypy gate)**: Requires Phases 1–3 complete
- **Phase 5 (US4 cleanup)**: Requires Phase 4 (mypy must pass before deleting files)
- **Phase 6 (Polish)**: Requires Phase 5 complete

### User Story Dependencies

- **US3 (taste batch)**: Delivered by Phase 1 — independently testable after T006
- **US1 (multi-place save)**: Delivered by Phase 2 — depends on Phase 1
- **US2 (provisional)**: Delivered by Phase 3 — depends on Phase 2 (shares `ExtractionService.run()`)
- **US4 (dead code)**: Delivered by Phase 5 — depends on Phases 1–4

### Parallel Opportunities Within Phase 1

T001, T002, T003, T004, T005, T006 all touch different files — run in parallel:

```
Task: "Update PlaceSaved in events.py"                   → T001
Task: "Add comment to ExtractionPending in types.py"      → T002
Task: "Update on_place_saved in handlers.py"             → T003
Task: "Update handle_place_saved in taste/service.py"    → T004
Task: "Create tests/core/events/test_events.py"          → T005
Task: "Update test_service_integration.py"               → T006
```

### Parallel Opportunities Within Phase 2

T009 (schema) and T012 (test_persistence) can start immediately. T010 (persistence.py) and T011 (service.py) must be sequential (service imports from persistence). T009 must complete before T011 (service imports `SavedPlace`).

```
Immediate: T009 (schema) + T012 (test_persistence)
Then:      T007 → T008 (bulk upsert — same file, sequential)
Then:      T010 (persistence, uses bulk upsert + schema)
Then:      T011 (service, imports persistence + schema)
Then:      T013 (test_service) + T014 (deps.py) + T015 (test_extract_place) — parallel
```

---

## Implementation Strategy

### MVP (Phase 1 + Phase 2 only)

1. Complete Phase 1: PlaceSaved migration
2. Complete Phase 2: Persistence service + ExtractionService rewrite + API schema
3. **STOP and validate**: `POST /v1/extract-place` returns `places: [...]` with multi-place shape
4. Phase 3 (provisional) can follow independently

### Full Delivery Order

Phase 1 → Phase 2 → Phase 3 → Phase 4 (mypy gate) → Phase 5 → Phase 6

---

## Notes

- **No `@pytest.mark.asyncio`** — `asyncio_mode = "auto"` in pytest config
- **Git comment char is `;`** not `#`
- **Circular dep**: T018 constructs `ExtractionPersistenceService` inline in `get_event_dispatcher` — do NOT add `Depends(get_extraction_persistence)` to its signature
- **T008 SQL**: Must use `sqlalchemy.dialects.postgresql.insert` + `on_conflict_do_update` — not generic SQLAlchemy insert
- **T010 ordering**: `PlaceSaved` dispatch AFTER all DB writes, BEFORE `bulk_upsert_embeddings` call — this ordering is invariant
- **NestJS coordination**: Do not deploy Phase 2 to production before NestJS client updated for new `ExtractPlaceResponse` shape
