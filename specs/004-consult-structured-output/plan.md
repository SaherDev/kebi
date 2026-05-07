# Implementation Plan: Consult Endpoint — Structured Output (Phase 2)

**Branch**: `004-consult-structured-output` | **Date**: 2026-03-25 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `specs/004-consult-structured-output/spec.md`

## Summary

Build a working `POST /v1/consult` endpoint that parses intent from a natural language query using GPT-4o-mini (via Instructor for structured extraction), builds 6 reasoning steps with real data in summaries, generates placeholder recommendation content, and returns a `ConsultResponse` matching the API contract exactly. Phase 2 scope: no pgvector, no Google Places, no ranking. Langfuse tracing on all LLM calls per ADR-025.

## Technical Context

**Language/Version**: Python 3.11 (>=3.11,<3.14)
**Primary Dependencies**: FastAPI 0.115, Pydantic 2.10, Instructor 1.x, OpenAI SDK (via instructor), Langfuse (new dep), httpx 0.28
**Storage**: N/A — Phase 2 writes no data to DB
**Testing**: pytest 8.3 + pytest-asyncio 0.25, `asyncio_mode = "auto"`
**Target Platform**: Linux server (Railway)
**Project Type**: web-service (FastAPI)
**Performance Goals**: Response under 10s for typical queries in development
**Constraints**: mypy --strict must pass; ruff check must pass; all existing tests must continue to pass
**Scale/Scope**: Single endpoint, Phase 2 (no retrieval/ranking)

## Constitution Check

*GATE: Must pass before implementation. Re-check after Phase 1 design.*

| ADR | Check | Status |
|-----|-------|--------|
| ADR-017 | Pydantic schemas for all request/response — `ConsultRequest`, `ConsultResponse`, `PlaceResult`, `ReasoningStep` all Pydantic `BaseModel` | ✓ PASS — existing schemas extend; `photos` field added |
| ADR-018 | Separate router module `routes/consult.py` | ✓ PASS — already exists |
| ADR-020 | No hardcoded model names — `get_llm("intent_parser")`, `get_llm("orchestrator")`, `get_instructor_client("intent_parser")` only | ✓ PASS — role-based access only |
| ADR-021 | consult uses LangGraph StateGraph | ⚠️ PHASE 2 DEFERRAL — see Complexity Tracking |
| ADR-025 | Langfuse callback on every LLM call | ✓ PASS — `providers/tracing.py` created; calls wrapped in generation spans |
| ADR-034 | Route handler makes exactly one service call | ✓ PASS — `service.consult()` only, nothing else in route file |
| ADR-038 | Protocol abstraction for swappable deps | ✓ PASS — `LLMClientProtocol` already defined; `IntentParser` uses `InstructorClient` via factory |
| ADR-003 | ruff + mypy strict | ✓ PASS — all new code must pass gates |
| ADR-004 | pytest in `tests/` mirroring `src/` structure | ✓ PASS — new test at `tests/core/intent/test_intent_parser.py` |

## Project Structure

### Documentation (this feature)

```text
specs/004-consult-structured-output/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
└── tasks.md             # Phase 2 output (/speckit.tasks command)
```

### Source Code Changes

```text
; NEW files
src/kebi/
├── core/intent/
│   ├── __init__.py                          ; (new)
│   └── intent_parser.py                     ; (new) ParsedIntent model + IntentParser class
└── providers/
    └── tracing.py                            ; (new) get_langfuse_client() factory

tests/
└── core/intent/
    ├── __init__.py                           ; (new)
    └── test_intent_parser.py                 ; (new)

kebi-config/bruno/ai-service/
└── consult.bru                               ; (new) sync JSON request

; MODIFIED files
src/kebi/
├── api/
│   ├── schemas/consult.py                   ; add photos to PlaceResult, rename SyncConsultResponse
│   └── routes/consult.py                    ; update ConsultResponse type reference
└── core/consult/
    └── service.py                            ; full implementation of consult()

pyproject.toml                                ; add langfuse dependency

tests/
├── api/test_consult.py                      ; update sync test for new response shape (photos)
└── core/consult/test_service.py             ; update stub assertions, add intent parser mock
```

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|--------------------------------------|
| ADR-021: LangGraph deferred — using sequential service instead of StateGraph | Phase 2 scope excludes retrieval, discovery, and ranking. All 3 LangGraph-parallel steps (ADR-009) are stubs. | LangGraph with 6 stub nodes adds ~200 lines of scaffolding (state schema, graph compilation, node wiring) that will be fully replaced in Phase 3 when real nodes are added. Net effect is zero functional difference with high implementation cost. Phase 3 introduces the StateGraph when retrieval and discovery are real. |

---

## Implementation Phases

### Phase A: Schema + Provider foundation

**Goal**: Add `photos` field to `PlaceResult`, rename `SyncConsultResponse` → `ConsultResponse`, add Langfuse dep + tracing module.

**Checklist**:
- [ ] A1: In `api/schemas/consult.py`:
  - Add `photos: list[str] = Field(min_length=1)` to `PlaceResult`
  - Rename class `SyncConsultResponse` → `ConsultResponse`
  - Keep all other fields identical
- [ ] A2: In `api/routes/consult.py`:
  - Update import: `SyncConsultResponse` → `ConsultResponse`
  - Update `responses` dict model reference
- [ ] A3: In `pyproject.toml`:
  - Add `langfuse = "^2.0"` to `[tool.poetry.dependencies]`
  - Run `poetry add langfuse` to update `poetry.lock`
- [ ] A4: Create `src/kebi/providers/tracing.py`:
  ```python
  """Langfuse tracing factory (ADR-025)."""
  import logging
  from typing import Any

  logger = logging.getLogger(__name__)

  def get_langfuse_client() -> Any | None:
      """Return Langfuse client or None if not configured.

      Returns None (with a warning) when Langfuse SDK is missing or
      credentials are absent. Callers must handle None gracefully.
      """
      try:
          import langfuse  ; noqa: PLC0415
          client = langfuse.Langfuse()
          client.auth_check()
          return client
      except Exception as exc:
          logger.warning("Langfuse tracing disabled: %s", exc)
          return None
  ```
- [ ] A5: Update `providers/__init__.py` to export `get_langfuse_client`
- [ ] A6: In `config/app.yaml`, add consult service config under `consult:`:
  ```yaml
  consult:
    max_alternatives: 2                                        ; Phase 2: always 2
    placeholder_photo_url: "https://placehold.co/800x450.webp" ; Phase 2: static placeholder
    response_timeout_seconds: 10                               ; Return 500 if LLM call exceeds this
  ```
  - Verify `models.intent_parser` is already mapped to openai/gpt-4o-mini (should already exist)
  - Verify `models.orchestrator` is already mapped (should already exist)

**Verify A**: `poetry run ruff check src/` and `poetry run mypy src/` pass.

---

### Phase B: Intent Parser

**Goal**: New `core/intent/intent_parser.py` with `ParsedIntent` Pydantic model and `IntentParser` class using `get_instructor_client("intent_parser")`.

**Checklist**:
- [ ] B1: Create `src/kebi/core/intent/__init__.py` (empty)
- [ ] B2: Create `src/kebi/core/intent/intent_parser.py`:
  - `ParsedIntent(BaseModel)` with fields:
    - `cuisine: str | None` — e.g., "ramen", "sushi", None if not specified
    - `occasion: str | None` — e.g., "date night", "quick lunch"
    - `price_range: str | None` — "low", "mid", "high", or None
    - `radius: int | None` — preferred search radius in meters, or None
    - `constraints: list[str]` — dietary, access, or other requirements (default `[]`)
  - `IntentParser` class:
    - `__init__(self)`: calls `get_instructor_client("intent_parser")` internally
    - `async def parse(self, query: str) -> ParsedIntent`: extracts intent via Instructor
    - System prompt: "You are an intent extraction assistant. Extract structured intent from a restaurant or place recommendation query. Return null for fields not mentioned."
    - Wraps call with Langfuse generation span if client available
    - `ValidationError` from Pydantic propagates to FastAPI as 422 (do not catch)
- [ ] B3: Create `tests/core/intent/__init__.py` (empty)
- [ ] B4: Create `tests/core/intent/test_intent_parser.py`:
  - `test_parse_returns_parsed_intent`: mock `get_instructor_client`, verify `ParsedIntent` returned
  - `test_parse_extracts_cuisine_and_occasion`: verify field values from mock response
  - `test_parse_returns_null_for_missing_fields`: verify `None` fields when not in query
  - `test_parse_propagates_validation_error`: mock raising `ValidationError`, verify it propagates

**Verify B**: `poetry run pytest tests/core/intent/ -v` passes.

---

### Phase C: ConsultService Full Implementation

**Goal**: Replace stub `consult()` method with real intent parsing + 6 reasoning steps + LLM-generated recommendations with placeholder photos.

**Checklist**:
- [ ] C1: Update `src/kebi/core/consult/service.py`:
  - Remove stub `consult()` body
  - Import `IntentParser` from `core.intent.intent_parser`
  - Import `get_langfuse_client` from `providers.tracing`
  - New `consult()` implementation:
    1. Instantiate `IntentParser()` and call `await parser.parse(query)` → `ParsedIntent`
    2. Build `intent_summary` from `ParsedIntent` fields (non-null only):
       `"Parsed: cuisine=ramen, occasion=date night"` (only include present fields)
    3. Build helper `_build_summary(step, intent, location)` — fills patterns from `ParsedIntent`:
       - cuisine fallback: `"restaurants"` when `intent.cuisine is None`
       - location fallback: `"nearby"` when request `location` is None
       - occasion fallback: `"your criteria"` when `intent.occasion is None`
       - radius fallback: `1.2` when `intent.radius is None`
    4. Build 6 `ReasoningStep` objects using `_build_summary`:
       - `intent_parsing`: `"Parsed: cuisine=ramen, occasion=date night"` (non-null fields only; omit null fields entirely from the string)
       - `retrieval`: `"Looking for [cuisine] places you've saved near [location]"`
       - `discovery`: `"Searching for [cuisine] restaurants within [radius]km of your location"`
       - `validation`: `"Checking which [cuisine] spots are open now"`
       - `ranking`: `"Comparing [cuisine] options for [occasion]"`
       - `completion`: `"Found your match"`
       **Rule**: no phase names, deferral language, or implementation state in any summary
    5. Call `self._llm.complete(messages)` with orchestrator to generate recommendation text
       (messages: system prompt from config + user query enriched with intent fields)
       Wrap with Langfuse generation span
    6. Parse response to extract `place_name`, `address`, `reasoning` (JSON-structured prompt for reliable parsing)
    7. Build `ConsultResponse` with `primary` + exactly 2 `alternatives`, all with `photos=[config.consult.placeholder_photo_url]`
       (Read `config.consult.placeholder_photo_url` and `config.consult.max_alternatives` from `get_config()`)
    8. Return `ConsultResponse`
- [ ] C2: Update imports in `service.py` (remove unused `json` import if no longer needed)
- [ ] C3: Update `tests/core/consult/test_service.py`:
  - Update assertions from stub values (`"Stub Place"`, 2-step reasoning) to new shapes
  - Mock `IntentParser.parse()` to return controlled `ParsedIntent` (cuisine="ramen", occasion="date night")
  - Verify all 6 reasoning steps present in correct order
  - Verify step summaries contain intent-derived values (no "deferred" / phase language)
  - Verify `len(result.alternatives) == 2` (exactly 2)
  - Verify `photos` field present and non-empty on `primary` and each alternative
  - Keep streaming tests unchanged (streaming mode not modified)
- [ ] C4: Update `tests/api/test_consult.py`:
  - Update `test_synchronous_endpoint_returns_json` to assert `photos` in `data["primary"]`
  - Add assertion `len(data["alternatives"]) == 2`
  - Update assertions for 6 reasoning steps

**Verify C**: `poetry run pytest tests/core/consult/ tests/api/test_consult.py -v` passes.

---

### Phase D: Bruno file + Full verification

**Goal**: Create Bruno sync consult request. Run all quality gates. Verify end-to-end.

**Checklist**:
- [ ] D1: Create `kebi-config/bruno/ai-service/consult.bru`:
  ```
  meta {
    name: Consult (Sync)
    type: http
    seq: 2
  }

  post {
    url: {{ai_url}}/v1/consult
    body: json
    auth: none
  }

  headers {
    Content-Type: application/json
  }

  body:json {
    {
      "user_id": "user-123",
      "query": "good ramen near Sukhumvit for a date night",
      "location": {
        "lat": 13.7563,
        "lng": 100.5018
      }
    }
  }

  tests {
    test("status is 200", function(res) {
      expect(res.status).to.equal(200);
    });

    test("primary recommendation present", function(res) {
      const data = res.body;
      expect(data.primary).to.exist;
      expect(data.primary.place_name).to.be.a('string');
      expect(data.primary.photos).to.be.an('array').with.length.greaterThan(0);
    });

    test("reasoning_steps has 6 entries", function(res) {
      expect(res.body.reasoning_steps).to.have.length(6);
    });
  }
  ```
- [ ] D2: Run full test suite: `poetry run pytest` — all tests pass
- [ ] D3: Run linter: `poetry run ruff check src/ tests/` — zero violations
- [ ] D4: Run type checker: `poetry run mypy src/` — zero errors
- [ ] D5: Start dev server and verify with curl:
  ```bash
  curl -X POST http://localhost:8000/v1/consult \
    -H "Content-Type: application/json" \
    -d '{"user_id":"test","query":"good ramen near Sukhumvit for a date night","location":{"lat":13.75,"lng":100.50}}'
  ```
  Verify: HTTP 200, JSON with `primary.photos`, 6 `reasoning_steps` in order

**Verify D**: All 4 quality gates pass. Bruno file added.

---

## Quality Gates (all must pass before done)

```bash
poetry run pytest          ; all tests pass (including pre-existing tests)
poetry run ruff check src/ tests/   ; zero violations
poetry run mypy src/       ; zero errors
```

---

## Notes

- **Rename impact**: `SyncConsultResponse` → `ConsultResponse` touches 4 files: schema, route, service, 2 test files. Update all in Phase A.
- **Config fields**: Phase A adds `consult:` section to `app.yaml` with `max_alternatives` (always 2 for Phase 2), `placeholder_photo_url` (read in Phase C), `response_timeout_seconds` (operational limit). All read via `get_config()` in service — never hardcoded.
- **Streaming tests**: The existing streaming tests in `test_consult.py` and `test_service.py` must continue to pass. Do not modify streaming mode.
- **Langfuse None handling**: All callers must check `if lf is not None` before using the client. mypy will enforce this if typed as `langfuse.Langfuse | None`.
- **Instructor ValidationError**: Do NOT catch `ValidationError` in `IntentParser.parse()`. Let it propagate to FastAPI's exception handler, which returns 422.
- **Photos placeholder**: Read from `config.consult.placeholder_photo_url` (defaults to `"https://placehold.co/800x450.webp"`). Publicly accessible, no auth needed.
- **Bruno location**: The existing Bruno file is at `kebi-config/bruno/ai-service/` (not `kebi-config/bruno/consult/` as originally specified). New file goes in the same `ai-service/` directory.
