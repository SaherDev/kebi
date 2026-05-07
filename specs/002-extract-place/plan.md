# Implementation Plan: Place Extraction Endpoint (Phase 2)

**Branch**: `002-extract-place` | **Date**: 2026-03-24 | **Spec**: [spec.md](spec.md)

## Summary

Build `POST /v1/extract-place` as a sequential async workflow (ADR-008, no LangGraph). The pipeline: route handler (facade) → `ExtractionService` → `ExtractionDispatcher` → extractor (TikTok or plain text) → Instructor/GPT-4o-mini → `GooglePlacesClient` → `compute_confidence()` → deduplication check → DB write or confirmation response.

Each extractor returns an `ExtractionResult` carrying both the structured `PlaceExtraction` and the `ExtractionSource` enum value. The service never re-inspects the raw input after dispatch — source classification is owned entirely by the extractor.

## Technical Context

**Language/Version**: Python 3.11 (>=3.11,<3.14)
**Primary Dependencies**: FastAPI 0.115, Pydantic 2.10, SQLAlchemy 2.0 async, Instructor 1.x (new), httpx 0.28 (promote to prod)
**Storage**: PostgreSQL via asyncpg + SQLAlchemy async. Alembic migration adds 3 columns to `places`.
**Testing**: pytest + pytest-asyncio. httpx TestClient for route tests.
**Target Platform**: Linux server (Railway)
**Performance Goals**: TikTok extraction < 10s total (spec SC-002); plain text < 5s total (spec SC-003); oEmbed timeout = 3s (clarification session 2026-03-24)
**Constraints**: No embeddings written in this task (ADR-040 handled separately). No LangGraph (ADR-008).
**Scale/Scope**: Phase 2 — TikTok + plain text only. Instagram/OCR/Whisper are Phase 3.

> **SC-002/SC-003 note**: These identifiers refer to the Success Criteria in `spec.md`, not ADR numbers. The 10s and 5s targets live in the spec; they are not separately recorded in `decisions.md`.

## Constitution Check

| ADR | Rule | Status |
|-----|------|--------|
| ADR-008 | extract-place is sequential async, NOT LangGraph | ✓ Pass — service is a plain async function |
| ADR-017 | Pydantic schemas for all request/response bodies | ✓ Pass — `ExtractPlaceRequest`, `ExtractPlaceResponse`, `PlaceExtraction`, `ExtractionResult` defined |
| ADR-018 | Separate router module: `routes/extract_place.py` | ✓ Pass — new file, not added to `main.py` body |
| ADR-020 | Provider abstraction — `get_instructor_client(role)` factory, no hardcoded model names | ✓ Pass — wrapper reads from `models.yaml` |
| ADR-022 | Google Places behind `PlacesClient` Protocol | ✓ Pass — `GooglePlacesClient` implements `PlacesClient` Protocol |
| ADR-023 | HTTP error mapping: 400/422/500 | ✓ Pass — `errors.py` maps domain exceptions to HTTP codes |
| ADR-025 | Langfuse on all LLM calls | ✓ Pass — `InstructorClient.extract()` attaches Langfuse handler |
| ADR-034 | Route handler is a facade — one service call, ≤30 lines | ✓ Pass — `extract_place.py` calls `ExtractionService.run()` only |
| ADR-038 | Protocol for every swappable dependency | ✓ Pass — `InputExtractor`, `PlacesClient`, `LLMClientProtocol` all defined as Protocols |
| ADR-040 | No OpenAI embeddings — embeddings not written in this task | ✓ Pass — no embedding calls in this pipeline |

**Constitution note:** ADR-030 is the authoritative decision — Alembic owns AI-table migrations in this repo. Alembic adds the new migration here.

**Complexity tracking:** No violations requiring justification.

## Project Structure

### Documentation (this feature)

```text
specs/002-extract-place/
├── plan.md              ← this file
├── research.md          ← Phase 0 output
├── data-model.md        ← Phase 1 output
├── contracts/
│   └── extract_place.md ← Phase 1 output
└── tasks.md             ← Phase 2 output (/speckit.tasks)
```

### Source Code

```text
src/kebi/
├── api/
│   ├── main.py                          ← add extract_place router inclusion + error handlers
│   ├── deps.py                          ← NEW: build_dispatcher() factory
│   ├── errors.py                        ← NEW: exception handlers (400/422/500)
│   ├── routes/
│   │   ├── consult.py                   ← unchanged
│   │   └── extract_place.py             ← NEW: facade handler
│   └── schemas/
│       ├── consult.py                   ← unchanged
│       └── extract_place.py             ← NEW: ExtractPlaceRequest/Response/PlaceExtraction
├── core/
│   └── extraction/
│       ├── __init__.py                  ← NEW
│       ├── protocols.py                 ← NEW: InputExtractor Protocol (returns ExtractionResult)
│       ├── dispatcher.py                ← NEW: ExtractionDispatcher + UnsupportedInputError
│       ├── confidence.py                ← NEW: ExtractionSource enum, compute_confidence()
│       ├── places_client.py             ← NEW: PlacesClient Protocol + GooglePlacesClient
│       ├── service.py                   ← NEW: ExtractionService.run()
│       └── extractors/
│           ├── __init__.py              ← NEW
│           ├── tiktok.py                ← NEW: TikTokExtractor (returns ExtractionResult with source=CAPTION)
│           └── plain_text.py            ← NEW: PlainTextExtractor (returns ExtractionResult with source=PLAIN_TEXT)
├── db/
│   └── models.py                        ← add google_place_id, confidence, source columns
└── providers/
    └── llm.py                           ← add InstructorClient + get_instructor_client()

tests/
├── api/
│   └── test_extract_place.py            ← NEW
└── core/
    └── extraction/
        ├── __init__.py                  ← NEW
        ├── test_confidence.py           ← NEW
        ├── test_dispatcher.py           ← NEW
        ├── test_tiktok_extractor.py     ← NEW
        └── test_plain_text_extractor.py ← NEW

alembic/versions/
    └── <hash>_add_extraction_metadata_to_places.py  ← NEW migration

config/
    └── .local.yaml                      ← add extraction.confidence_weights section
```

---

## Implementation Phases

### Phase A: Dependencies + DB

**A1 — Add `instructor` to production deps in `pyproject.toml`**
- Add `instructor = "^1.0"` under `[tool.poetry.dependencies]`
- Promote `httpx = "^0.28"` from `[tool.poetry.group.dev.dependencies]` to `[tool.poetry.dependencies]`
- Run `poetry lock && poetry install`

**A2 — Add columns to `Place` model (`src/kebi/db/models.py`)**
- Add `google_place_id: Mapped[str | None]`
- Add `confidence: Mapped[float | None]`
- Add `source: Mapped[str | None]`

**A3 — Generate and write Alembic migration**
- `poetry run alembic revision --autogenerate -m "add_extraction_metadata_to_places"`
- Verify the generated file adds the 3 columns and creates `ix_places_google_place_id` index
- Run `poetry run alembic upgrade head` against local DB

---

### Phase B: Schemas

**B1 — `src/kebi/api/schemas/extract_place.py`**
- `PlaceExtraction(BaseModel)` — LLM output schema with `place_name`, `address`, `cuisine`, `price_range`
- `ExtractPlaceRequest(BaseModel)` — `user_id: str`, `raw_input: str`
- `ExtractPlaceResponse(BaseModel)` — `place_id: str | None`, `place: PlaceExtraction`, `confidence: float`, `requires_confirmation: bool`, `source_url: str | None`

---

### Phase C: Extraction layer (core)

**C1 — `src/kebi/core/extraction/confidence.py`**
- `ExtractionSource(str, Enum)` — `CAPTION`, `PLAIN_TEXT`, `SPEECH`, `OCR`
- `compute_confidence(source, places_match, corroborated) -> float`
  - Reads all weights from `load_yaml_config(".local.yaml")["extraction"]["confidence_weights"]`
  - Applies: base score → Places modifier → multi-source bonus → NONE cap (max 0.30) → overall max cap (0.95)
  - Pure function — no side effects, no I/O

**C2 — `src/kebi/core/extraction/protocols.py`**

`InputExtractor` Protocol — `extract()` returns `ExtractionResult | None`, not bare `PlaceExtraction | None`. The extractor owns its source classification; the service never re-inspects the raw input.

```python
from typing import Protocol
from kebi.core.extraction.result import ExtractionResult

class InputExtractor(Protocol):
    async def extract(self, raw_input: str) -> ExtractionResult | None: ...
    def supports(self, raw_input: str) -> bool: ...
```

**C2a — `src/kebi/core/extraction/result.py`** *(new small module)*

```python
from pydantic import BaseModel
from kebi.api.schemas.extract_place import PlaceExtraction
from kebi.core.extraction.confidence import ExtractionSource

class ExtractionResult(BaseModel):
    extraction: PlaceExtraction
    source: ExtractionSource
    source_url: str | None  # populated by TikTokExtractor, None for PlainTextExtractor
```

**C3 — `src/kebi/core/extraction/places_client.py`**
- `PlacesMatchQuality(str, Enum)` — `EXACT`, `FUZZY`, `CATEGORY_ONLY`, `NONE`
- `PlacesMatchResult(BaseModel)` — `match_quality`, `validated_name`, `google_place_id`, `lat`, `lng`
- `PlacesClient(Protocol)` — `async def validate_place(self, name: str, location: str | None) -> PlacesMatchResult`
- `GooglePlacesClient` — implements `PlacesClient`, reads `GOOGLE_PLACES_API_KEY` from `os.environ`, calls `findplacefromtext` via `httpx.AsyncClient`, computes match quality with `difflib.SequenceMatcher`

**C4 — `src/kebi/providers/llm.py` additions**
- `InstructorClient` — wraps `instructor.from_openai(AsyncOpenAI(...))`, holds config-resolved `model` string, exposes `async def extract(self, response_model, messages, max_retries=3)` with all three Instructor exception types handled
- `get_instructor_client(role: str) -> InstructorClient` factory — reads `models.yaml` the same way `get_llm()` does

**C5 — `src/kebi/core/extraction/extractors/tiktok.py`**
- `TikTokExtractor` — implements `InputExtractor`
- `supports()`: `urllib.parse.urlparse(raw_input).netloc` contains `"tiktok.com"`
- `extract()`: async httpx GET to `https://www.tiktok.com/oembed?url={raw_input}` with 3s timeout, extracts `title` field, passes to `self._instructor_client.extract(PlaceExtraction, [...])`, returns `ExtractionResult(extraction=result, source=ExtractionSource.CAPTION, source_url=raw_input)`

**C6 — `src/kebi/core/extraction/extractors/plain_text.py`**
- `PlainTextExtractor` — implements `InputExtractor`
- `supports()`: `parsed.scheme not in ("http", "https")`
- `extract()`: passes `raw_input` directly to `self._instructor_client.extract(PlaceExtraction, [...])`, returns `ExtractionResult(extraction=result, source=ExtractionSource.PLAIN_TEXT, source_url=None)`

**C7 — `src/kebi/core/extraction/dispatcher.py`**
- `UnsupportedInputError(Exception)`
- `ExtractionDispatcher` — `__init__(self, extractors: list[InputExtractor])`, `async def dispatch(self, raw_input: str) -> ExtractionResult | None` — iterates extractors in order, first `supports()` match wins, raises `UnsupportedInputError` if none match. No classification logic inside the dispatcher.

**C8 — `src/kebi/core/extraction/service.py`**

`ExtractionService` — `__init__(self, dispatcher, places_client, db_session_factory)`

`async def run(self, raw_input: str, user_id: str) -> ExtractPlaceResponse`:

1. Validate `raw_input` not empty → raise `ValueError` (→ 400)
2. `dispatcher.dispatch(raw_input)` → `ExtractionResult | None`; on `UnsupportedInputError` → raise (→ 422)
3. If `None` → raise `ExtractionFailedNoMatchError` (→ 422)
4. `places_client.validate_place(result.extraction.place_name, result.extraction.address)` → `PlacesMatchResult`
5. `compute_confidence(result.source, match.match_quality, corroborated=False)` — source comes from `ExtractionResult`, not re-derived from raw input
6. Threshold: `≤ 0.30` → raise `ExtractionFailedNoMatchError` (→ 422)
7. Threshold: `< 0.70` → return `ExtractPlaceResponse(place_id=None, requires_confirmation=True, ...)`
8. **Deduplication before write**: if `match.google_place_id is not None`, query DB for existing `Place` with `google_place_id = match.google_place_id`. If found, return that record immediately without writing a duplicate. Two users saving the same restaurant share one `Place` row.
9. Write new `Place` to DB: `id = str(uuid4())`, populate `google_place_id`, `confidence`, `source` from `result.source.value`
10. Return `ExtractPlaceResponse(place_id=place.id, requires_confirmation=False, confidence=confidence, ...)`

---

### Phase D: API layer

**D1 — `src/kebi/api/errors.py`**
- Register FastAPI exception handlers: `ValueError` → 400, `ExtractionFailedNoMatchError` → 422, `UnsupportedInputError` → 422, unhandled `Exception` → 500
- All return `{"error_type": "...", "detail": "..."}` JSON body

**D2 — `src/kebi/api/deps.py`**
- `build_dispatcher() -> ExtractionDispatcher` — creates `TikTokExtractor` and `PlainTextExtractor` with `get_instructor_client("intent_parser")`, returns `ExtractionDispatcher([tiktok, plain_text])`
- `get_extraction_service()` FastAPI dependency — creates `ExtractionService` with `build_dispatcher()`, `GooglePlacesClient()`, and DB session from `Depends(get_db_session)`

**D3 — `src/kebi/api/routes/extract_place.py`**
- `router = APIRouter()`
- `@router.post("/extract-place")` — receives `ExtractPlaceRequest`, calls `service.run(body.raw_input, body.user_id)`, returns `ExtractPlaceResponse`. Under 30 lines. No SQLAlchemy, no httpx, no Instructor calls inside the route file.

**D4 — `src/kebi/api/main.py`**
- Import `extract_place_router` from `routes/extract_place`
- Add `router.include_router(extract_place_router, prefix="")`
- Register error handlers from `errors.py` on `app`

---

### Phase E: Config + Docs

**E1 — `config/.local.yaml`**
- Add `extraction.confidence_weights` section with all base scores, Places modifiers, multi-source bonus, and max score
- Add `extraction.thresholds` with `store_silently: 0.70` and `require_confirmation: 0.30`

**E2 — `docs/api-contract.md`**
- Update `/v1/extract-place` response to include `requires_confirmation` field
- Update confidence threshold note from 0.50 to 0.70
- Add `source_url` field description
- Add error response body shape with `error_type`

---

### Phase F: Tests

**F1 — `tests/core/extraction/test_confidence.py`**
- Unit tests for `compute_confidence()`: all source/match combinations, NONE cap, multi-source bonus, max cap

**F2 — `tests/core/extraction/test_dispatcher.py`**
- `UnsupportedInputError` raised when no extractor matches
- Correct extractor selected for TikTok URL
- Correct extractor selected for plain text
- Order respected (TikTok before plain text)
- `ExtractionResult` returned contains correct `source` field

**F3 — `tests/core/extraction/test_tiktok_extractor.py`**
- `supports()` true for `tiktok.com` URLs, false for others
- `extract()` with mocked httpx response returns `ExtractionResult` with `source=CAPTION`
- Timeout behaviour: `httpx.TimeoutException` propagates correctly

**F4 — `tests/core/extraction/test_plain_text_extractor.py`**
- `supports()` true for non-URL strings, false for http/https URLs
- `extract()` with mocked instructor client returns `ExtractionResult` with `source=PLAIN_TEXT`

**F5 — `tests/api/test_extract_place.py`**
- 200 with place saved (confidence ≥ 0.70, new record)
- 200 with deduplication — existing `google_place_id` returns existing `place_id` without DB write
- 200 with `requires_confirmation: true` (0.30 < confidence < 0.70)
- 422 `extraction_failed_no_match` on confidence ≤ 0.30
- 422 `unsupported_input` on non-TikTok URL
- 400 on empty `raw_input`
- Mock `ExtractionService.run()` in all route tests

---

### Phase G: Bruno

**G1 — Bruno request file**
- Path: `kebi-config/bruno/extract-place.bru`
- Request: `POST {{baseUrl}}/v1/extract-place` with TikTok URL body
- Expected response: 200 with `place_id`, `confidence`, `requires_confirmation`

---

## Verify commands

```bash
poetry run pytest tests/core/extraction/ -v
poetry run pytest tests/api/test_extract_place.py -v
poetry run pytest -x
poetry run ruff check src/ tests/
poetry run ruff format src/ tests/
poetry run mypy src/
poetry run alembic upgrade head
```

All must pass before marking complete.

## Implementation order

```
A1 → A2 → A3
B1
C1 → C2 → C2a → C3 → C4   (parallelizable)
C5 depends on C2, C2a, C4
C6 depends on C2, C2a, C4
C7 depends on C2
C8 depends on C3, C5, C6, C7
D1 → D2 → D3 → D4
E1 → E2
F1 → F2 → F3 → F4 → F5
G1
```
