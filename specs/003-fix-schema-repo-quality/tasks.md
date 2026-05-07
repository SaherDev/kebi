---
description: "Task list for Schema, Repository, and Code Quality Fixes feature"
---

# Tasks: Schema, Repository, and Code Quality Fixes

**Input**: Design documents from `/specs/003-fix-schema-repo-quality/`
**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md
**Branch**: `003-fix-schema-repo-quality`
**Total Tasks**: 30 | **Commits**: 10

**Tests**: Test tasks included (requested via spec success criteria SC-001 through SC-007)

**Organization**: Tasks are grouped by user story to enable independent implementation and testing.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3, US4)
- Include exact file paths in descriptions

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Initialize Alembic and project configuration

- [ ] T001 Initialize Alembic migrations framework with `poetry run alembic init migrations`
- [ ] T002 Configure `migrations/env.py` to use asyncpg URL from `get_secrets().database.url` and load `Base` from `kebi.db.base`

**Checkpoint**: Alembic ready for schema migration

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [ ] T003 Update `Place` model in `src/kebi/db/models.py`: replace `google_place_id` with `external_provider` (NOT NULL) and `external_id` (nullable), add UniqueConstraint on the pair
- [ ] T004 [P] Create `PlaceRepository` Protocol in `src/kebi/db/repositories/place_repository.py` with `get_by_provider()` and `save()` methods
- [ ] T005 [P] Create `SQLAlchemyPlaceRepository` implementation in `src/kebi/db/repositories/place_repository.py` with try/except/rollback in `save()` and structured error logging
- [ ] T006 [P] Create `src/kebi/db/repositories/__init__.py` and export `PlaceRepository` and `SQLAlchemyPlaceRepository`
- [ ] T007 Add explicit rollback to `get_session()` in `src/kebi/db/session.py`: wrap yield in try/except with `await session.rollback()` on exception
- [ ] T008 Create Alembic migration `migrations/versions/001_provider_agnostic_place_identity.py` with: (1) add columns with defaults, (2) backfill existing data (`external_provider='google'`, copy `google_place_id` to `external_id`), (3) drop default from `external_provider`, (4) create partial unique index, (5) drop `google_place_id` column

**Checkpoint**: Foundation ready - user story implementation can now begin in parallel

---

## Phase 3: User Story 1 - Multi-Provider Place Identity (Priority: P1) 🎯 MVP

**Goal**: Replace provider-locked `google_place_id` with a `(external_provider, external_id)` composite key so the system can support multiple place data sources (Google, Yelp, Foursquare, etc.) without schema conflicts.

**Independent Test**: Submit two places—one from Google, one from a different provider—and verify both persist without collision. Re-submit the same place from the same provider and verify the record is updated (not duplicated).

### Tests for User Story 1

- [ ] T009 [P] [US1] Create test file structure: `tests/db/__init__.py`, `tests/db/repositories/__init__.py`, `tests/db/repositories/test_place_repository.py`
- [ ] T010 [P] [US1] Unit test `test_save_new_place` in `tests/db/repositories/test_place_repository.py`: verify new place is inserted and returned
- [ ] T011 [P] [US1] Unit test `test_save_existing_place_updates_mutable_fields` in `tests/db/repositories/test_place_repository.py`: verify re-saving same `(provider, external_id)` updates all mutable fields (name, address, cuisine, price_range, lat, lng, source_url, validated_at, confidence, source)
- [ ] T012 [P] [US1] Unit test `test_get_by_provider_returns_existing` in `tests/db/repositories/test_place_repository.py`: verify correct record is returned
- [ ] T013 [P] [US1] Unit test `test_save_skips_dedup_when_external_id_is_none` in `tests/db/repositories/test_place_repository.py`: verify place with null external_id always inserts (no dedup attempt)
- [ ] T014 [US1] Integration test `test_multi_provider_place_identity` in `tests/api/test_extract_place.py`: submit two places (one Google, one different provider), verify both saved, re-submit same provider/id and verify update

### Implementation for User Story 1

- [ ] T015 [P] [US1] Update `PlacesMatchResult` in `src/kebi/core/extraction/places_client.py`: rename `google_place_id` → `external_id`, add `external_provider: str = "google"` default
- [ ] T016 [P] [US1] Update `GooglePlacesClient.validate_place()` in `src/kebi/core/extraction/places_client.py` to set `external_id=...` (was `google_place_id=...`)
- [ ] T017 [US1] Refactor `ExtractionService` in `src/kebi/core/extraction/service.py`: replace constructor param `db_session: AsyncSession` with `place_repo: PlaceRepository`, update docstring to reference `(external_provider, external_id)`, update step 6 dedup to use `place_repo.get_by_provider()`, update step 7 to create Place with `external_provider` and `external_id` fields, remove raw `session.add()` + `session.commit()` calls
- [ ] T018 [US1] Update `get_extraction_service()` in `src/kebi/api/deps.py`: wire `SQLAlchemyPlaceRepository(db_session)` instead of passing `db_session` directly
- [ ] T019 [US1] Update all existing tests in `tests/api/test_extract_place.py` and `tests/core/extraction/test_service.py` to pass a `MagicMock` or `AsyncMock` implementing `PlaceRepository` instead of `db_session`
- [ ] T020 [US1] Update all test assertions in extraction tests to use `external_id` instead of `google_place_id`

**Checkpoint**: User Story 1 (Multi-Provider Identity) is complete and independently testable

---

## Phase 4: User Story 2 - Reliable Place Persistence with Error Recovery (Priority: P2)

**Goal**: Ensure that when a place save operation fails, the transaction is rolled back completely with no partial writes, and the caller receives a clear structured error with context for debugging.

**Independent Test**: Simulate a database error during save (e.g., connection loss), verify no partial record exists in DB afterward, and verify the caller receives a RuntimeError with context (provider, external_id, error details).

### Tests for User Story 2

- [ ] T021 [P] [US2] Unit test `test_save_rollback_on_commit_failure` in `tests/db/repositories/test_place_repository.py`: mock session to raise exception on commit, verify rollback is called and RuntimeError is raised with context
- [ ] T022 [P] [US2] Unit test `test_save_does_not_update_immutable_fields` in `tests/db/repositories/test_place_repository.py`: verify id, user_id, external_provider, external_id, created_at are never updated on upsert
- [ ] T023 [P] [US2] Unit test `test_get_by_provider_returns_none_for_unknown` in `tests/db/repositories/test_place_repository.py`: verify None is returned when no match exists
- [ ] T024 [US2] Integration test `test_error_recovery_on_save_failure` in `tests/api/test_extract_place.py`: submit place to extraction endpoint when DB is down, verify error response and no partial record left in DB

### Implementation for User Story 2

- [ ] T025 [US2] Verify `SQLAlchemyPlaceRepository.save()` in `src/kebi/db/repositories/place_repository.py` has complete error handling: try/except catching all exceptions, explicit `await session.rollback()`, structured error logging with `external_provider`, `external_id`, `error` in extra dict, and re-raising as `RuntimeError` with context
- [ ] T026 [US2] Verify `get_session()` in `src/kebi/db/session.py` has explicit rollback in exception handler (already added in Phase 2, verify here)
- [ ] T027 [US2] Update API error handlers in `src/kebi/api/errors.py` to return structured error response with context when `RuntimeError` from repository is caught
- [ ] T028 [US2] Run existing test suite to ensure all 40 existing tests still pass after error handling changes

**Checkpoint**: User Story 2 (Error Recovery) is complete and independently testable

---

## Phase 5: User Story 3 - Complete and Accurate API Documentation (Priority: P3)

**Goal**: Ensure the OpenAPI specification accurately reflects the actual API behavior (status codes and response schemas) and that all documentation states the correct embedding dimensions.

**Independent Test**: Open the auto-generated OpenAPI docs at `/docs`, verify the consult endpoint shows status 200 with a documented response schema. Verify `docs/api-contract.md` states embedding dimension as 1024, matching the code.

### Tests for User Story 3

- [ ] T029 [P] [US3] Integration test `test_consult_openapi_documentation` in `tests/api/test_consult.py`: verify `/openapi.json` includes status 200 and response schema for consult endpoint
- [ ] T030 [P] [US3] Documentation test `test_api_contract_accuracy` in `tests/docs/test_contracts.py`: read `docs/api-contract.md` and verify embedding dimension is 1024, matches code

### Implementation for User Story 3

- [ ] T031 [US3] Update `@router.post` decorator in `src/kebi/api/routes/consult.py`: add `status_code=200, responses={200: {"description": "Synchronous recommendation response (stream=false)", "model": SyncConsultResponse}}`. Do NOT set `response_model` (breaks StreamingResponse)
- [ ] T032 [US3] Update `docs/api-contract.md`: find all occurrences of embedding dimension `1536` in the embeddings section and replace with `1024`
- [ ] T033 [US3] Confirm in PR description that pgvector columns remain owned exclusively by this repo's Alembic migrations (1024 dimensions)

**Checkpoint**: User Story 3 (API Documentation) is complete and independently testable

---

## Phase 6: User Story 4 - Stable Deployment Health Checks (Priority: P4)

**Goal**: Ensure the hosting platform can reliably detect service health and readiness via the health check endpoint, preventing traffic from being routed to unhealthy instances.

**Independent Test**: Deploy to staging environment, verify the platform health probe at `/v1/health` succeeds and returns 200, and traffic is routed to the instance.

### Tests for User Story 4

- [ ] T034 [P] [US4] Deployment smoke test: verify Railway deploys successfully and platform health check passes within 30 seconds
- [ ] T034 [P] [US4] Health endpoint test in `tests/api/test_health.py`: verify `GET /v1/health` returns 200 OK

### Implementation for User Story 4

- [ ] T035 [US4] Add `healthcheckPath = "/v1/health"` to `[deploy]` section in `railway.toml`
- [ ] T036 [US4] Verify health endpoint exists in `src/kebi/api/main.py` at `GET /v1/health` (should already exist, verify in code)

**Checkpoint**: User Story 4 (Deployment Health Checks) is complete and independently testable

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Final improvements and provider abstraction fixes affecting multiple layers

- [ ] T037 [P] Export `get_instructor_client` from `src/kebi/providers/__init__.py`: add to imports and `__all__` list
- [ ] T038 [P] Update import in `src/kebi/api/deps.py` from `from kebi.providers.llm import get_instructor_client` to `from kebi.providers import get_instructor_client` (fixes M2)
- [ ] T039 [P] Add `# type: ignore[import-untyped]` comment after `import instructor` on line 7 of `src/kebi/providers/llm.py` (fixes L2)
- [ ] T040 Run verification suite: `poetry run pytest` (all tests pass), `poetry run ruff check src/ tests/`, `poetry run ruff format --check src/ tests/`, `poetry run mypy src/`
- [ ] T041 Run migration verification: `poetry run alembic upgrade head`, `poetry run alembic downgrade -1`, `poetry run alembic upgrade head` (requires running DB)

**Checkpoint**: All tasks complete, ready for final integration testing

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — can start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1 completion — **BLOCKS all user stories**
- **Phase 3-6 (User Stories)**: All depend on Phase 2 completion
  - User stories can proceed in parallel or sequentially
  - Each story is independently testable after its phase completes
- **Phase 7 (Polish)**: Depends on all user stories being complete

### User Story Dependencies

- **US1 (P1 - Multi-Provider Identity)**: Depends on Phase 2 only — can start immediately after foundational
- **US2 (P2 - Error Recovery)**: Depends on Phase 2 and US1 (uses repository pattern from US1)
- **US3 (P3 - API Documentation)**: Depends on Phase 2 only — independent from US1/US2
- **US4 (P4 - Deployment Health)**: Depends on Phase 2 only — independent from US1/US2/US3

### Within Each Phase

- Tests (if included) MUST be written and FAIL before implementation
- Create file structure before implementing
- Models/repositories before services
- Services before API routes
- Core implementation before integration tests

### Parallel Opportunities

**Phase 2** - Can parallelize:
- T004, T005, T006 (Repository files can be created in parallel)
- T003, T007, T008 (Models, session, migration can be done in parallel)

**Once Phase 2 completes, can run in parallel**:
- US1 Phase 3 (Tests T009-T014, Implementation T015-T020)
- US3 Phase 5 (Tests T029-T030, Implementation T031-T033)
- US4 Phase 6 (Tests T034, Implementation T035-T036)

US2 should start after US1 implementation (uses PlaceRepository from US1).

---

## Parallel Example: Phase 2 Foundational

```
Developer A: T003 (Update Place model)
Developer B: T004 + T005 + T006 (Repository Protocol + Implementation + exports)
Developer C: T007 + T008 (Session rollback + Alembic migration)

Once all complete, all developers can proceed to user stories.
```

---

## Parallel Example: User Stories 1 & 3 (Post-Phase 2)

```
Developer A: US1 (Tests T009-T014, Implementation T015-T020)
Developer B: US3 (Tests T029-T030, Implementation T031-T033)

These stories are independent and can be completed in parallel.
US2 waits for US1 implementation to complete.
US4 can start after Phase 2, independent of all other stories.
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup ✅
2. Complete Phase 2: Foundational (CRITICAL) ✅
3. Complete Phase 3: User Story 1 (P1 - Multi-Provider Identity)
4. **STOP and VALIDATE**: Run `poetry run pytest` on US1 tests, verify all pass
5. Deploy to staging and test independently

### Incremental Delivery

1. Complete Phase 1 + Phase 2 → Foundation ready (no user-facing change yet)
2. Add Phase 3 (US1) → Multi-provider places work → Deploy & demo
3. Add Phase 4 (US2) → Error recovery works → Deploy & demo (non-breaking)
4. Add Phase 5 (US3) → Documentation accurate → Deploy (documentation-only change)
5. Add Phase 6 (US4) → Health checks working → Deploy (operational fix)
6. Complete Phase 7 → Polish complete → Ready for production

### Suggested Commit Points

After each phase completion (10 commits total):

```
1. refactor(db): add external_provider/external_id columns, Alembic migration for places (Phase 1 + Phase 2 T003-T008)
2. refactor(db): add PlaceRepository Protocol + SQLAlchemyPlaceRepository (Phase 2 T004-T006)
3. fix(db): explicit rollback in get_session on exception (Phase 2 T007)
4. refactor(extraction): use PlaceRepository in ExtractionService, update PlacesMatchResult (Phase 3 T015-T020)
5. test(db): add PlaceRepository unit tests (Phase 3 T009-T014)
6. test(api): add extraction integration tests for US1 (Phase 3 T014)
7. fix(api): add status_code and responses docs to consult endpoint (Phase 5 T031)
8. fix(docs): update embedding dimension from 1536 to 1024 in api-contract.md (Phase 5 T032)
9. chore(config): add healthcheckPath to railway.toml, fix provider exports, add type ignore (Phase 6 T035, Phase 7 T037-T039)
10. test: add comprehensive error recovery and documentation tests (Phase 4 + Phase 5 + Phase 6)
```

---

## Notes

- [P] tasks = different files, no immediate dependencies
- [Story] label (US1, US2, US3, US4) maps task to specific user story for traceability
- Each user story should be independently completable and testable after its phase
- Verify tests fail before implementing (TDD approach)
- Run verify commands after each phase: `poetry run pytest`, `poetry run ruff check`, `poetry run mypy`
- Stop at any checkpoint to validate story independently before proceeding
- Avoid: vague tasks, same file conflicts that block parallelization
- Database must be running for integration tests and migration verification (use `docker compose up -d`)
