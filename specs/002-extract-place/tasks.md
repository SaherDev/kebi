# Implementation Tasks: Place Extraction Endpoint (Phase 2)

**Feature**: Extract Place Endpoint (`/v1/extract-place`)
**Branch**: `002-extract-place`
**Created**: 2026-03-24
**Plan**: [plan.md](plan.md) | [Spec](spec.md) | [Data Model](data-model.md) | [API Contract](contracts/extract_place.md)

---

## Implementation Strategy

**MVP Scope (US1)**: TikTok extraction endpoint with confidence-based save-or-confirm logic. Covers Phase 1 (Setup) + Phase 2 (Foundational) + Phase 3 (US1 TikTok). Phase 4 and 5 extend to plain text and confirmation refinement.

**Execution Model**:
- Phases 1–2 are strictly sequential (blocking prerequisites).
- Phase 3 (US1) and Phase 4 (US2) can partially parallelize after Phase 2 completes (extractors are independent).
- Phase 5 (US3) reuses Phase 3 logic; tests added incrementally.

**Test Strategy**: Tests are organized by component (confidence, dispatcher, extractors, API). TDD not required; tests accompany or follow implementation. All must pass before integration.

---

## Phase 1: Setup (Project Initialization)

### Goal
Add production dependencies and extend the database schema to store extraction metadata.

### Independent Test Criteria
- `poetry lock` succeeds without conflicts
- Alembic migration applies without errors
- New columns appear in database schema
- Rollback migration succeeds

---

- [X] T001 Add `instructor` and promote `httpx` in `pyproject.toml`

  **File**: `pyproject.toml`
  **Task**: Add `instructor = "^1.0"` to `[tool.poetry.dependencies]`. Promote `httpx = "^0.28"` from `[tool.poetry.group.dev.dependencies]` to main. Run `poetry lock && poetry install`.

- [X] T002 Add three columns to `Place` model

  **File**: `src/kebi/db/models.py`
  **Task**: Add to `Place` class:
  ```python
  google_place_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
  confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
  source: Mapped[str | None] = mapped_column(String, nullable=True)
  ```

- [X] T003 Generate and verify Alembic migration

  **File**: `alembic/versions/<hash>_add_extraction_metadata_to_places.py`
  **Task**: Run `poetry run alembic revision --autogenerate -m "add_extraction_metadata_to_places"`. Verify the generated file adds the three columns and creates `ix_places_google_place_id` index. Run `poetry run alembic upgrade head` locally. Test rollback with `poetry run alembic downgrade -1`.

---

## Phase 2: Foundational (Blocking Prerequisites)

### Goal
Build shared extraction infrastructure: schemas, protocols, clients, confidence logic, and error handling. All components needed by all user stories.

### Independent Test Criteria
- All Pydantic models import without errors
- All Protocols are syntactically valid
- Confidence computation produces scores 0.0–0.95
- Dispatcher raises `UnsupportedInputError` when no extractor matches
- Error handlers return correct HTTP codes

---

- [X] T004 Create Pydantic schemas for extract-place endpoint

  **File**: `src/kebi/api/schemas/extract_place.py`
  **Task**: Define four Pydantic models:
  - `PlaceExtraction` — LLM output with `place_name`, `address`, `cuisine` (optional), `price_range` (optional)
  - `ExtractPlaceRequest` — `user_id: str`, `raw_input: str`
  - `ExtractPlaceResponse` — `place_id: str | None`, `place: PlaceExtraction`, `confidence: float`, `requires_confirmation: bool`, `source_url: str | None`

- [X] T005 [P] Create extraction result and confidence modules

  **File**: `src/kebi/core/extraction/result.py`
  **Task**: Define `ExtractionResult(BaseModel)` with:
  - `extraction: PlaceExtraction`
  - `source: ExtractionSource` (enum)
  - `source_url: str | None`

  **File**: `src/kebi/core/extraction/confidence.py`
  **Task**: Define:
  - `ExtractionSource(str, Enum)` — `CAPTION`, `PLAIN_TEXT`, `SPEECH`, `OCR`
  - `compute_confidence(source: ExtractionSource, match_quality: PlacesMatchQuality, corroborated: bool) -> float` — pure function, reads weights from config, applies base score → Places modifier → multi-source bonus → NONE cap → max cap (0.95)

- [X] T006 [P] Create extraction protocols

  **File**: `src/kebi/core/extraction/protocols.py`
  **Task**: Define `InputExtractor` Protocol:
  ```python
  async def extract(self, raw_input: str) -> ExtractionResult | None: ...
  def supports(self, raw_input: str) -> bool: ...
  ```

- [X] T007 [P] Create places client protocol and implementation

  **File**: `src/kebi/core/extraction/places_client.py`
  **Task**: Define:
  - `PlacesMatchQuality(str, Enum)` — `EXACT` (≥0.95 similarity), `FUZZY` (≥0.80), `CATEGORY_ONLY`, `NONE`
  - `PlacesMatchResult(BaseModel)` — `match_quality`, `validated_name`, `google_place_id`, `lat`, `lng`
  - `PlacesClient(Protocol)` — `async def validate_place(name: str, location: str | None) -> PlacesMatchResult`
  - `GooglePlacesClient(PlacesClient)` — calls `findplacefromtext`, reads `GOOGLE_PLACES_API_KEY` from environment, computes match quality with `difflib.SequenceMatcher`

- [X] T008 [P] Create Instructor client wrapper and factory

  **File**: `src/kebi/providers/llm.py`
  **Task**: Add to existing file:
  - `InstructorClient` — wraps `instructor.from_openai(AsyncOpenAI(...))`, exposes `async def extract(response_model, messages, max_retries=3)` with Instructor exception handling
  - `get_instructor_client(role: str) -> InstructorClient` factory — reads model name from `models.yaml`, instantiates Instructor client

- [X] T009 Create extraction dispatcher

  **File**: `src/kebi/core/extraction/dispatcher.py`
  **Task**: Define:
  - `UnsupportedInputError(Exception)`
  - `ExtractionDispatcher` — `__init__(self, extractors: list[InputExtractor])`, `async def dispatch(raw_input: str) -> ExtractionResult | None` — iterates extractors, first `supports()` match wins, raises `UnsupportedInputError` if none match

- [X] T010 [P] Create HTTP error handlers

  **File**: `src/kebi/api/errors.py`
  **Task**: Define FastAPI exception handlers for:
  - `ValueError` → 400 `bad_request`
  - `UnsupportedInputError` → 422 `unsupported_input`
  - `ExtractionFailedNoMatchError` → 422 `extraction_failed_no_match`
  - Unhandled `Exception` → 500 `extraction_error`

  Each returns `{"error_type": "...", "detail": "..."}`

- [X] T011 [P] Create API dependencies module

  **File**: `src/kebi/api/deps.py`
  **Task**: Define:
  - `build_dispatcher() -> ExtractionDispatcher` — creates TikTok and plain text extractors with `get_instructor_client("intent_parser")`, returns `ExtractionDispatcher([tiktok, plain_text])`
  - `get_extraction_service()` FastAPI dependency — creates `ExtractionService` with dispatcher, `GooglePlacesClient()`, and DB session

- [X] T012 Create core extraction __init__.py

  **File**: `src/kebi/core/extraction/__init__.py`
  **Task**: Create empty marker file (or export public classes if desired)

- [X] T013 Create extractors __init__.py

  **File**: `src/kebi/core/extraction/extractors/__init__.py`
  **Task**: Create empty marker file

- [X] T014 Create unit tests for confidence scoring

  **File**: `tests/core/extraction/test_confidence.py`
  **Task**: Test `compute_confidence()` with all source/match combinations, NONE cap, multi-source bonus, max cap. Verify scores stay within 0.0–0.95 range.

- [X] T015 Create unit tests for dispatcher

  **File**: `tests/core/extraction/test_dispatcher.py`
  **Task**: Test:
  - `UnsupportedInputError` raised when no extractor matches
  - Correct extractor selected for TikTok URL
  - Correct extractor selected for plain text
  - Order respected (TikTok before plain text)
  - `ExtractionResult` contains correct `source` field

- [X] T016 Create tests __init__.py files

  **File**: `tests/core/extraction/__init__.py`
  **Task**: Create empty marker files for test package

---

## Phase 3: User Story 1 (P1 — TikTok Extraction)

### Story Goal
A user pastes a TikTok video URL. The system extracts the caption, identifies the restaurant, validates it, and saves the place record or requests confirmation.

### Independent Test Criteria
- TikTok extractor identifies `tiktok.com` URLs
- TikTok oEmbed call completes within 3 seconds
- Extracted place data passes schema validation
- Service saves to database when confidence ≥ 0.70
- API returns 200 with `place_id` and `requires_confirmation: false` on success
- Mock all external calls (httpx, Google Places, LLM) in route tests

---

- [X] T017 [US1] Create TikTok extractor

  **File**: `src/kebi/core/extraction/extractors/tiktok.py`
  **Task**: Implement `TikTokExtractor(InputExtractor)`:
  - `supports(raw_input)`: `urllib.parse.urlparse(raw_input).netloc` contains `"tiktok.com"`
  - `extract(raw_input)`: httpx GET to `https://www.tiktok.com/oembed?url={raw_input}` with 3s timeout, extract `title`, pass to `self._instructor_client.extract(PlaceExtraction, [...])`, return `ExtractionResult(extraction=result, source=ExtractionSource.CAPTION, source_url=raw_input)`

- [X] T018 [US1] Create extraction service

  **File**: `src/kebi/core/extraction/service.py`
  **Task**: Implement `ExtractionService`:
  - `__init__(self, dispatcher, places_client, db_session_factory)`
  - `async def run(raw_input, user_id) -> ExtractPlaceResponse`:
    1. Validate `raw_input` not empty → raise `ValueError` (→ 400)
    2. Dispatch → `ExtractionResult | None`; on `UnsupportedInputError` → raise (→ 422)
    3. If `None` → raise `ExtractionFailedNoMatchError` (→ 422)
    4. Validate place against Google Places
    5. Compute confidence from `result.source` + match quality
    6. Threshold ≤ 0.30 → raise `ExtractionFailedNoMatchError` (→ 422)
    7. Threshold < 0.70 → return `requires_confirmation=True` (no write)
    8. Threshold ≥ 0.70 → dedup check by `google_place_id`, write new `Place` if needed, return `place_id`

- [X] T019 [US1] Create extract-place API route

  **File**: `src/kebi/api/routes/extract_place.py`
  **Task**: Implement:
  - `router = APIRouter()`
  - `@router.post("/extract-place")` receives `ExtractPlaceRequest`, calls `service.run(body.raw_input, body.user_id)`, returns `ExtractPlaceResponse`. Under 30 lines.

- [X] T020 [US1] Include extract-place router in main app

  **File**: `src/kebi/api/main.py`
  **Task**: Import `extract_place_router` and add `app.include_router(extract_place_router, prefix="")`. Register error handlers from `errors.py`.

- [X] T021 [US1] Create TikTok extractor unit tests

  **File**: `tests/core/extraction/test_tiktok_extractor.py`
  **Task**: Test:
  - `supports()` true for `tiktok.com` URLs, false for others
  - `extract()` with mocked httpx response returns `ExtractionResult` with `source=CAPTION`
  - Timeout behavior: `httpx.TimeoutException` propagates
  - Result includes correct `source_url`

- [X] T022 [US1] Create API integration tests

  **File**: `tests/api/test_extract_place.py`
  **Task**: Test:
  - 200 with place saved (confidence ≥ 0.70, new record)
  - 200 with deduplication — existing `google_place_id` returns existing `place_id` without write
  - 200 with `requires_confirmation: true` (0.30 < confidence < 0.70)
  - 422 `extraction_failed_no_match` on confidence ≤ 0.30
  - 422 `unsupported_input` on non-TikTok URL
  - 400 on empty `raw_input`

  Mock `ExtractionService.run()` in all route tests.

---

## Phase 4: User Story 2 (P2 — Plain Text Extraction)

### Story Goal
A user types a restaurant name and optionally a location. The system extracts the data, validates it, and saves the place or requests confirmation. Reuses all infrastructure from US1.

### Independent Test Criteria
- Plain text extractor accepts non-URL strings
- Plain text extractor rejects http/https URLs
- Extracted place data passes schema validation
- Service handles plain text flow the same as TikTok (confidence, thresholds)
- API route works for both TikTok and plain text inputs

---

- [X] T023 [US2] Create plain text extractor

  **File**: `src/kebi/core/extraction/extractors/plain_text.py`
  **Task**: Implement `PlainTextExtractor(InputExtractor)`:
  - `supports(raw_input)`: `urllib.parse.urlparse(raw_input).scheme not in ("http", "https")`
  - `extract(raw_input)`: pass `raw_input` directly to `self._instructor_client.extract(PlaceExtraction, [...])`, return `ExtractionResult(extraction=result, source=ExtractionSource.PLAIN_TEXT, source_url=None)`

- [X] T024 [US2] Create plain text extractor unit tests

  **File**: `tests/core/extraction/test_plain_text_extractor.py`
  **Task**: Test:
  - `supports()` true for non-URL strings, false for http/https URLs
  - `extract()` with mocked instructor client returns `ExtractionResult` with `source=PLAIN_TEXT`
  - Result has `source_url=None`

---

## Phase 5: User Story 3 (P3 — Confirmation Logic & Thresholds)

### Story Goal
When confidence is low (0.30–0.70), the system returns a candidate place without saving. When confidence ≤ 0.30, the system returns an error. User confirms or corrects, and calls the endpoint again.

### Independent Test Criteria
- Service applies correct thresholds: ≥0.70 save, 0.30–0.70 confirm, ≤0.30 error
- Deduplication works: same `google_place_id` returns existing record
- Response shapes match contract for all three outcomes
- All confidence score branches tested

### Notes
- Confirmation logic is implemented in Phase 3 (`ExtractionService`); Phase 5 adds focused tests.
- No new code files; existing Phase 3 logic covers all branches.

---

- [X] T025 [US3] Extend API tests for confirmation flow

  **File**: `tests/api/test_extract_place.py` (extend T022)
  **Task**: Add test cases:
  - Confidence in 0.30–0.70 range returns `requires_confirmation=true` and `place_id=null`
  - Confidence ≤ 0.30 returns 422 error
  - Threshold values match `config/.local.yaml` (read from config in tests)

---

## Phase 6: Polish & Cross-Cutting Concerns

### Goal
Configure thresholds, document API contract, and add integration testing via Bruno.

### Independent Test Criteria
- Config file loads without errors
- All documented thresholds match code
- Bruno request succeeds against running server
- API contract matches implementation

---

- [X] T026 Create confidence configuration file

  **File**: `config/.local.yaml` (create if missing, or update)
  **Task**: Add `extraction` section:
  ```yaml
  extraction:
    confidence_weights:
      base_scores:
        CAPTION: 0.70
        PLAIN_TEXT: 0.70
        SPEECH: 0.60
        OCR: 0.55
      places_modifiers:
        EXACT: 0.20
        FUZZY: 0.15
        CATEGORY_ONLY: 0.10
        NONE_CAP: 0.30
      multi_source_bonus: 0.10
      max_score: 0.95
    thresholds:
      store_silently: 0.70
      require_confirmation: 0.30
  ```

- [X] T027 Update API contract documentation

  **File**: `docs/api-contract.md`
  **Task**: Add/update section for `/v1/extract-place`:
  - Request/response shapes
  - Error codes and trigger conditions
  - Confidence threshold note (0.70 for save, 0.30 for confirmation, ≤0.30 for error)
  - Timeout behavior (3s for TikTok oEmbed)

- [X] T028 Create Bruno request file for extract-place

  **File**: `kebi-config/bruno/extract-place.bru`
  **Task**: Create request:
  - `POST {{baseUrl}}/v1/extract-place`
  - Body: `{ "user_id": "test-user", "raw_input": "<TikTok URL or plain text>" }`
  - Expected response: 200 with `place_id`, `confidence`, `requires_confirmation`

---

## Verification & Completion

### Test Commands

All must pass before marking complete:

```bash
poetry run pytest tests/core/extraction/ -v
poetry run pytest tests/api/test_extract_place.py -v
poetry run pytest -x
poetry run ruff check src/ tests/
poetry run ruff format src/ tests/
poetry run mypy src/
poetry run alembic upgrade head
docker compose up -d
```

### Definition of Done

- [X] All tasks completed (checkboxes above)
- [X] All tests pass (`pytest -x`) — 40 tests passing
- [X] Code passes linting (`ruff check`)
- [X] Code passes type checking (`mypy src/`)
- [X] Database migration applied (`alembic upgrade head`)
- [X] Endpoint tested in Bruno — confirmed working with plain text input

---

## Task Dependencies & Parallel Execution

### Execution Order

```
Phase 1 (Setup)
  T001 → T002 → T003

Phase 2 (Foundational)
  T004
  T005, T006, T007, T008 [P] (parallelizable after T004)
  T009 (depends on T005, T006)
  T010, T011, T012, T013 [P] (parallelizable)
  T014, T015, T016 [P] (tests, can run after prerequisites)

Phase 3 (US1 TikTok)
  T017 (depends on T008, T005, T006)
  T018 (depends on T017, T007, T009)
  T019 (depends on T018, T004)
  T020 (depends on T019, T010)
  T021 (depends on T017)
  T022 (depends on T020)

Phase 4 (US2 Plain text)
  T023 (depends on T008, T005, T006)
  T024 (depends on T023)

Phase 5 (US3 Confirmation)
  T025 (depends on T022, T018)

Phase 6 (Polish)
  T026 (can run after Phase 2)
  T027 (can run after Phase 1)
  T028 (can run after Phase 3)
```

### Parallel Opportunities

- **After T004 (schemas)**: T005–T008, T010–T013 are independent; parallelize all.
- **After T009 (dispatcher)**: T017 and T023 (extractors) are independent; parallelize.
- **After Phase 2 complete**: T026–T028 (config/docs) can run in parallel with Phase 3 implementation.

### Suggested MVP Path (Day 1)

Execute in order: T001 → T002 → T003 → T004 → {T005–T013 in parallel} → T009 → T017 → T018 → T019 → T020 → T021 → T022

**MVP completion**: US1 (TikTok) fully working with tests. Total ~7 hours, parallelizable to ~4 hours.

---

## Summary

| Phase | Story | Task Count | Est. Hours | Blockers |
|-------|-------|-----------|-----------|----------|
| 1 | Setup | 3 | 1 | None |
| 2 | Foundational | 13 | 5 | Phase 1 |
| 3 | US1 (TikTok) | 6 | 4 | Phase 2 |
| 4 | US2 (Plain text) | 2 | 1 | US1 extractors |
| 5 | US3 (Confirmation) | 1 | 0.5 | Phase 3 service |
| 6 | Polish | 3 | 1 | All above |
| **Total** | | **28** | **12.5** | Sequential setup |

