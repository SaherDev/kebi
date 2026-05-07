---
description: "Implementation task list for Consult Endpoint — Structured Output (Phase 2)"
---

# Tasks: Consult Endpoint — Structured Output (Phase 2)

**Branch**: `004-consult-structured-output`
**Date**: 2026-03-25
**Input**: Design documents from `specs/004-consult-structured-output/`
**Spec**: [spec.md](spec.md) | **Plan**: [plan.md](plan.md) | **Data Model**: [data-model.md](data-model.md)

## Format: `[ID] [P?] [Story?] Description with file path`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2, US3, US4)
- Include exact file paths in descriptions

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and dependency management

- [x] T001 Add `langfuse = "^2.0"` to `[tool.poetry.dependencies]` in `pyproject.toml` and run `poetry add langfuse`

**Checkpoint**: Langfuse dependency installed - ready for provider implementation

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented

⚠️ **CRITICAL**: No user story work can begin until this phase is complete

- [x] T002 [P] In `src/kebi/api/schemas/consult.py`: Add `photos: list[str] = Field(min_length=1)` field to `PlaceResult` class
- [x] T003 [P] In `src/kebi/api/schemas/consult.py`: Rename class `SyncConsultResponse` → `ConsultResponse`
- [x] T004 [P] In `src/kebi/api/routes/consult.py`: Update import `SyncConsultResponse` → `ConsultResponse` and update `responses` dict model reference
- [x] T005 Create `src/kebi/providers/tracing.py` with `get_langfuse_client()` factory function per plan Phase A4 specification
- [x] T006 Update `src/kebi/providers/__init__.py` to export `get_langfuse_client`
- [x] T007 In `config/app.yaml`, add consult service config under `consult:` section: `max_alternatives: 2`, `placeholder_photo_url: "https://placehold.co/800x450.webp"`, `response_timeout_seconds: 10`
- [x] T008 Verify `config/app.yaml` has `intent_parser` role mapped to openai/gpt-4o-mini under `models:`
- [x] T009 Verify `config/app.yaml` has `orchestrator` role already mapped (should exist from prior work)

**Verify Phase 2**: `poetry run ruff check src/ tests/` and `poetry run mypy src/` pass

**Checkpoint**: Foundation ready - user story implementation can now begin in parallel

---

## Phase 3: User Story 2 - Intent Extraction (Priority: P1)

**Goal**: Extract structured intent fields from natural language queries using Instructor and GPT-4o-mini

**Independent Test**: Call `IntentParser.parse()` with a raw query string and verify it returns a `ParsedIntent` Pydantic model with correct fields populated or null where not specified

### Implementation for User Story 2

- [x] T010 Create `src/kebi/core/intent/__init__.py` (empty init file)
- [x] T011 Create `src/kebi/core/intent/intent_parser.py` with:
  - `ParsedIntent(BaseModel)` with fields: `cuisine: str | None`, `occasion: str | None`, `price_range: str | None`, `radius: int | None`, `constraints: list[str] = []`
  - `IntentParser` class with `__init__` calling `get_instructor_client("intent_parser")` internally
  - `async def parse(self, query: str) -> ParsedIntent` method that extracts intent via Instructor
  - System prompt: "You are an intent extraction assistant. Extract structured intent from a restaurant or place recommendation query. Return null for fields not mentioned."
  - Wrap Instructor call with Langfuse generation span if client available (per plan Phase B2)
  - Let Pydantic `ValidationError` propagate to FastAPI as 422 (do not catch)
- [x] T012 Create `tests/core/intent/__init__.py` (empty init file)
- [x] T013 Create `tests/core/intent/test_intent_parser.py` with tests:
  - `test_parse_returns_parsed_intent`: mock `get_instructor_client`, verify `ParsedIntent` returned
  - `test_parse_extracts_cuisine_and_occasion`: verify field values from mock response
  - `test_parse_returns_null_for_missing_fields`: verify `None` fields when not in query
  - `test_parse_propagates_validation_error`: mock raising `ValidationError`, verify it propagates

**Verify Phase 3**: `poetry run pytest tests/core/intent/ -v` passes

**Checkpoint**: User Story 2 (Intent Extraction) is complete and independently testable

---

## Phase 4: User Stories 1 & 3 - Service Implementation (Priority: P1 & P2)

**Goal US1**: Return a complete structured recommendation with primary, 2 alternatives, and 6 reasoning steps using GPT-4o-mini

**Goal US3**: Ensure all reasoning step summaries contain real intent-derived values, never generic placeholders or "deferred" language

**Independent Test US1**: POST /v1/consult with valid query returns JSON with `primary` (PlaceResult), exactly 2 entries in `alternatives`, and all 6 `reasoning_steps` with correct identifiers

**Independent Test US3**: All step summaries (steps 2–5) contain intent-derived field values (cuisine, occasion, location context) using fallbacks when null

### Implementation for User Stories 1 & 3

- [x] T014 [US1][US3] Update `src/kebi/core/consult/service.py`:
  - Import `IntentParser` from `core.intent.intent_parser`
  - Import `get_langfuse_client` from `providers.tracing`
  - Import `get_config` from `core.config`
  - Replace stub `consult()` body with full implementation per plan Phase C1:
    1. Instantiate `IntentParser()` and call `await parser.parse(query)` → `ParsedIntent`
    2. Build `intent_summary` from `ParsedIntent` fields (non-null only): e.g., `"Parsed: cuisine=ramen, occasion=date night"`
    3. Create helper `_build_summary(step, intent, location)` — fills patterns from `ParsedIntent` using fallbacks:
       - cuisine fallback: `"restaurants"` when `intent.cuisine is None`
       - location fallback: `"nearby"` when request `location` is None
       - occasion fallback: `"your criteria"` when `intent.occasion is None`
       - radius fallback: `1.2` when `intent.radius is None`
    4. Build 6 `ReasoningStep` objects with exact summaries per plan Phase C (intent-derived, no phase language):
       - `intent_parsing`: `"Parsed: [fields]"` (non-null fields only)
       - `retrieval`: `"Looking for [cuisine] places you've saved near [location]"`
       - `discovery`: `"Searching for [cuisine] restaurants within [radius]km of your location"`
       - `validation`: `"Checking which [cuisine] spots are open now"`
       - `ranking`: `"Comparing [cuisine] options for [occasion]"`
       - `completion`: `"Found your match"`
    5. Call `self._llm.complete(messages)` with orchestrator to generate recommendation text
       Wrap with Langfuse generation span per ADR-025
    6. Parse response to extract `place_name`, `address`, `reasoning` (JSON-structured prompt)
    7. Build `ConsultResponse` with `primary` + exactly 2 `alternatives`, all with `photos=[config.consult.placeholder_photo_url]`
    8. Return `ConsultResponse`
- [x] T015 [US1][US3] Update `tests/core/consult/test_service.py`:
  - Update assertions from stub values to new shapes (6 reasoning steps, 2 alternatives)
  - Mock `IntentParser.parse()` to return controlled `ParsedIntent` (cuisine="ramen", occasion="date night")
  - Verify all 6 reasoning steps present in correct order
  - Verify step summaries contain intent-derived values (no "deferred" / phase language)
  - Verify `len(result.alternatives) == 2` (exactly 2)
  - Verify `photos` field present and non-empty on `primary` and each alternative
  - Keep streaming tests unchanged
- [x] T016 [US1] Update `tests/api/test_consult.py`:
  - Update `test_synchronous_endpoint_returns_json` to assert `photos` in `data["primary"]`
  - Add assertion `len(data["alternatives"]) == 2`
  - Update assertions to verify 6 reasoning steps in correct order

**Verify Phase 4**: `poetry run pytest tests/core/consult/ tests/api/test_consult.py -v` passes

**Checkpoint**: User Stories 1 & 3 are complete and independently testable

---

## Phase 5: User Story 4 - Integration & Validation (Priority: P2)

**Goal**: Verify request validation, error handling, and end-to-end functionality with Bruno request file

**Independent Test**: Send requests with missing/empty fields and verify 400/422/500 responses; run Bruno sync request and verify 200 response with correct shape

### Implementation for User Story 4

- [x] T017 Create `kebi-config/bruno/ai-service/consult.bru` with sync consult request per plan Phase D1:
  - POST to `{{ai_url}}/v1/consult`
  - Request body: `user_id: "user-123"`, `query: "good ramen near Sukhumvit for a date night"`, `location: {lat: 13.7563, lng: 100.5018}`
  - Tests: verify status 200, primary recommendation present with photos, 6 reasoning_steps
- [x] T018 Run full test suite: `poetry run pytest` — all tests pass (including pre-existing tests)
- [x] T019 Run linter: `poetry run ruff check src/ tests/` — zero violations
- [x] T020 Run type checker: `poetry run mypy src/` — zero errors
- [x] T021 Manual verification: Start dev server (`poetry run uvicorn kebi.api.main:app --reload`) and test with curl:
  ```bash
  curl -X POST http://localhost:8000/v1/consult \
    -H "Content-Type: application/json" \
    -d '{"user_id":"test","query":"good ramen near Sukhumvit for a date night","location":{"lat":13.75,"lng":100.50}}'
  ```
  Verify: HTTP 200, JSON with `primary.photos`, 6 `reasoning_steps` in order, step summaries contain parsed fields

**Verify Phase 5**: All 4 quality gates pass, Bruno file created, manual curl test succeeds

**Checkpoint**: User Story 4 (Validation & Integration) complete - all user stories working together

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Final verification and documentation updates

- [x] T022 [P] Run quickstart.md validation: Follow all steps in `specs/004-consult-structured-output/quickstart.md` end-to-end
- [x] T023 Update `CLAUDE.md` in project root with new technologies from plan (Langfuse, Instructor, OpenAI SDK) if not already present
- [x] T024 Verify all Langfuse tracing is working: Check that LLM calls are generating traces in Langfuse dashboard (if configured)
- [x] T025 Code cleanup: Remove any unused imports in modified files

**Verify Phase 6**: Quickstart validation passes, CLAUDE.md updated, all quality gates still passing

**Checkpoint**: Feature complete and ready for merge to `dev`

---

## Dependencies & Execution Order

### Phase Dependencies

```
Phase 1: Setup
    ↓
Phase 2: Foundational (BLOCKS all stories)
    ↓
├─→ Phase 3: US2 - Intent Extraction (P1)
│       ↓
│   Phase 4: US1 & US3 - Service Implementation (P1 & P2)
│       ↓
└─→ Phase 5: US4 - Integration & Validation (P2)
        ↓
    Phase 6: Polish & Cross-Cutting
```

### Task Dependencies

**Within Phase 2 (Foundational)**:
- T002, T003, T004 can run in parallel (different parts of same file)
- T005, T006 depend on langfuse being installed (T001)
- T007, T008, T009 are config verification (independent)

**Within Phase 3 (US2)**:
- T010 must complete before T011
- T012 must complete before T013
- Tests (T013) can run as soon as code (T011) is written

**Within Phase 4 (US1 & US3)**:
- T014 depends on Phase 2 (foundational) and Phase 3 (intent parser) being complete
- T015, T016 depend on T014 being complete
- Tests can run as soon as implementation is in place

**Within Phase 5 (US4)**:
- All tasks depend on Phase 4 being complete
- T018–T020 can run in parallel (ruff, mypy, pytest)
- T021 depends on dev server starting (manual step)

---

## Parallel Opportunities

### Setup & Foundational Phases

```bash
# After T001 (langfuse dependency):
Parallel: T005, T006, T007, T008, T009
Parallel within same phase: T002, T003, T004 (different schema fields)
```

### User Story Phases

```bash
# Once Phase 2 (Foundational) is complete:
# Phase 3 (US2 Intent Parser) and Phase 4 (US1/US3 Service) can overlap:
Task US2: T010, T011, T012, T013 (intent parser implementation + tests)
Task US1/US3: T014, T015, T016 (service implementation + tests)
# Run in parallel if team capacity allows; or sequentially if single developer

# Phase 5 (US4 Validation) depends on Phase 4 completion
```

### Quality Gates

```bash
# These can run in parallel (Phase 5):
Parallel: T018 (pytest), T019 (ruff check), T020 (mypy)
Sequential: T021 (manual curl test, depends on server starting)
```

---

## Implementation Strategy

### MVP First (User Stories 1 & 2 Only)

**Recommended for single developer or when time-constrained:**

1. ✅ Complete Phase 1: Setup (5 min)
2. ✅ Complete Phase 2: Foundational (30 min)
3. ✅ Complete Phase 3: US2 Intent Extraction (45 min)
4. ✅ Complete Phase 4: US1 Service Implementation (60 min)
5. 🛑 **STOP and VALIDATE**: Run `poetry run pytest`, curl test, verify primary recommendation works
6. Deploy/demo US1 + US2 as MVP (core functionality)
7. Later: Add US3 (data quality) and US4 (validation) in separate PR

**MVP Scope**: Queries parse correctly + endpoint returns valid recommendations ✅

### Incremental Delivery (All User Stories)

**Recommended for team with multiple developers:**

1. Team completes Phase 1 + Phase 2 together (foundation)
2. **Developer A** → Phase 3 (US2 Intent Extraction) + Phase 4 (US1 Service) = core recommendation
3. **Developer B** → Phase 4 (US3 Real Data in Summaries) = quality
4. **Developer C** → Phase 5 (US4 Error Validation) = robustness
5. All phases: Phase 6 (Polish)
6. Merge all together → feature complete

**Timeline**: ~4 hours total (with parallelization)

---

## Task Count Summary

| Phase | Name | Task Count | User Stories |
|-------|------|-----------|---------------|
| 1 | Setup | 1 | (shared) |
| 2 | Foundational | 8 | (shared) |
| 3 | US2 Intent Extraction | 4 | US2 (P1) |
| 4 | US1 & US3 Service | 3 | US1 (P1), US3 (P2) |
| 5 | US4 Validation | 5 | US4 (P2) |
| 6 | Polish | 4 | (cross-cutting) |
| **TOTAL** | | **25** | 4 stories |

### By Priority

| Priority | User Stories | Task Count |
|----------|--------------|-----------|
| **P1** | US1 (Recommendation), US2 (Intent) | 7 |
| **P2** | US3 (Real Data), US4 (Validation) | 8 |
| **Setup/Polish** | (shared) | 13 |

---

## Verification Checkpoints

Stop at each checkpoint to validate independently:

- ✅ **After Phase 2**: Schemas updated, config in place, providers ready
  ```bash
  poetry run ruff check src/ && poetry run mypy src/
  ```

- ✅ **After Phase 3**: Intent parser works
  ```bash
  poetry run pytest tests/core/intent/ -v
  ```

- ✅ **After Phase 4**: Endpoint returns valid recommendations with real data
  ```bash
  poetry run pytest tests/core/consult/ tests/api/test_consult.py -v
  curl -X POST http://localhost:8000/v1/consult -H "Content-Type: application/json" -d '{...}'
  ```

- ✅ **After Phase 5**: All validation and error handling working
  ```bash
  poetry run pytest
  poetry run ruff check src/ tests/
  poetry run mypy src/
  ```

- ✅ **After Phase 6**: Quickstart works end-to-end
  ```bash
  # Follow all steps in specs/004-consult-structured-output/quickstart.md
  ```

---

## Notes

- **[P] marker**: Task can run in parallel (different files, no blocking dependencies)
- **[Story] label**: Maps task to user story for traceability
- **Each user story is independently completable and testable** — can be deployed separately
- **Avoid**: Mixing stories in commits; rushing past checkpoints; assuming tasks are done before running tests
- **Golden rule**: Test fails → implement → test passes → commit → move to next task
