# Implementation Plan: Consult Pipeline

**Branch**: `015-consult-pipeline` | **Date**: 2026-04-09 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `specs/015-consult-pipeline/spec.md`

## Summary

Rewrite the consult pipeline as a sequential 6-step service: intent parsing → saved recall →
external discovery → conditional validation → ranking → response building. Resolves 10 known
conflicts (C1–C10) and introduces the `Candidate` model, `CandidateMapper` Protocol,
`core/places/` module, and deterministic reasoning. No LangGraph for this phase (deferred
to Phase 4 per ADR-050 below).

## Technical Context

**Language/Version**: Python 3.11
**Primary Dependencies**: FastAPI 0.115, Pydantic 2.10, instructor, openai SDK, langfuse, httpx, sqlalchemy async
**Storage**: PostgreSQL (existing places table — no new migrations)
**Testing**: pytest (asyncio_mode=auto)
**Target Platform**: Linux server (Railway)
**Project Type**: Web service
**Performance Goals**: Consult response in under 5 seconds end-to-end (SC-001)
**Constraints**: mypy --strict must pass; ruff check must pass
**Scale/Scope**: Sequential pipeline; parallel branches deferred to Phase 4

## Constitution Check

*GATE: Must pass before proceeding.*

| ADR | Status | Notes |
|-----|--------|-------|
| ADR-001 (src layout) | ✅ PASS | No layout changes |
| ADR-002 (hybrid dir) | ✅ PASS | core/places/ is a new domain module under core/ |
| ADR-003 (ruff+mypy) | ✅ PASS | Verify step required |
| ADR-004 (pytest) | ✅ PASS | New test files for all new modules |
| ADR-008 (extract-place sequential) | ✅ PASS | Not applicable to consult |
| ADR-009 (parallel branches) | ✅ DEFERRED | Sequential now, parallelize in Phase 4 |
| ADR-014 (/v1 prefix) | ✅ PASS | Unchanged |
| ADR-017 (Pydantic schemas) | ✅ PASS | All types are Pydantic models |
| ADR-019 (Depends()) | ✅ PASS | get_consult_service() wired via Depends in deps.py |
| ADR-020 (provider abstraction) | ✅ PASS | IntentParser uses get_instructor_client("intent_parser") |
| ADR-021 (LangGraph for consult) | ⚠️ SUPERSEDED | New ADR-050 below defers LangGraph to Phase 4 |
| ADR-022 (PlacesClient in extraction/) | ⚠️ SUPERSEDED | New ADR-049 below moves to core/places/ |
| ADR-023 (HTTP error mapping) | ✅ PASS | Intent failure → 500; provider failure → graceful fallback |
| ADR-025 (Langfuse on all LLM calls) | ✅ PASS | IntentParser already traces; new service doesn't add LLM calls |
| ADR-034 (route handler one service call) | ✅ PASS | consult() calls service.consult() only |
| ADR-038 (Protocol for swappable deps) | ✅ PASS | PlacesClient Protocol extended with discover() + validate() |
| ADR-044 (prompt injection) | ✅ PASS | Intent parser doesn't inject retrieved content |

### New ADRs Required

**ADR-049**: Move PlacesClient from `core/extraction/` to `core/places/`.
Supersedes ADR-022. Rationale: PlacesClient now serves two pipelines (extraction + consult);
neutral location prevents extraction from being a dependency of consult. All extraction
imports updated. No functional change.

**ADR-050**: Defer LangGraph to Phase 4. Supersedes ADR-021 for Phase 3 scope.
ConsultService is a plain sequential async class for this phase — no StateGraph, no tool
selection, no streaming. LangGraph will be introduced in Phase 4 alongside SSE streaming
and parallel Step 2/3 branches (ADR-009).

**GATE RESULT**: Proceed after ADR-049 and ADR-050 are added to `docs/decisions.md` in
Phase A (first task).

## Project Structure

### Documentation (this feature)

```text
specs/015-consult-pipeline/
├── plan.md              ← this file
├── research.md          ← Phase 0 output
├── data-model.md        ← Phase 1 output
├── quickstart.md        ← Phase 1 output
├── contracts/
│   └── consult-api.md   ← Phase 1 output
└── tasks.md             ← Phase 2 output (/speckit.tasks)
```

### Source Code

```text
src/kebi/
├── api/
│   ├── deps.py                          ← add get_consult_service() (C7)
│   ├── routes/
│   │   └── consult.py                   ← remove stream branch (C6)
│   └── schemas/
│       ├── consult.py                   ← remove stream, fix photos (C6, C9)
│       └── recall.py                    ← add lat, lng (C8)
├── core/
│   ├── consult/
│   │   ├── __init__.py
│   │   ├── service.py                   ← full rewrite (C5)
│   │   └── types.py                     ← new: Candidate, mappers
│   ├── intent/
│   │   └── intent_parser.py             ← + 3 ParsedIntent fields (C3)
│   ├── places/                          ← new module (C2)
│   │   ├── __init__.py
│   │   └── places_client.py             ← moved + discover() + validate() (C1, C2)
│   ├── ranking/
│   │   └── service.py                   ← new rank() signature (C4)
│   └── utils/
│       └── geo.py                       ← new: haversine_m()
├── db/repositories/
│   └── recall_repository.py             ← add lat/lng to SQL + TypedDict (C8)
└── config.py                            ← + RadiusDefaultsConfig (C3, C10)

config/
└── app.yaml                             ← + consult.radius_defaults (C10)

docs/
└── decisions.md                         ← + ADR-049, ADR-050
```

## Complexity Tracking

| Item | Why Needed | Simpler Alternative Rejected Because |
|------|------------|--------------------------------------|
| core/places/ new module | PlacesClient serves extraction + consult; must not live in either | Re-exporting from extraction/ creates a confusing double import path |
| CandidateMapper Protocol | Two distinct mapper implementations; Protocol enforces the shared interface | Single function per mapper is fine for one implementation but breaks when adding a third source |

---

## Phase A — ADRs, Config, and PlacesClient Move

*No behaviour changes. Infrastructure only.*

### A1 — Write ADR-049 and ADR-050 in `docs/decisions.md`

Add ADR-049 (PlacesClient location change, supersedes ADR-022) and ADR-050 (LangGraph
deferral, supersedes ADR-021) at the top of `docs/decisions.md`.

### A2 — Add `consult.radius_defaults` to `config/app.yaml`

```yaml
consult:
  max_alternatives: 2
  placeholder_photo_url: "..."
  response_timeout_seconds: 10
  radius_defaults:
    default: 2000
    nearby: 1000
    walking: 500
```

Also add `nearbysearch_url` to `external_services.google_places`:

```yaml
external_services:
  google_places:
    base_url: https://maps.googleapis.com/maps/api/place/findplacefromtext/json
    nearbysearch_url: https://maps.googleapis.com/maps/api/place/nearbysearch/json
    ...
```

### A3 — Add `RadiusDefaultsConfig` to `core/config.py`

```python
class RadiusDefaultsConfig(BaseModel):
    default: int = 2000
    nearby: int = 1000
    walking: int = 500

class ConsultConfig(BaseModel):
    max_alternatives: int = 2
    placeholder_photo_url: str = "..."
    response_timeout_seconds: int = 10
    radius_defaults: RadiusDefaultsConfig = RadiusDefaultsConfig()
```

### A4 — Create `core/places/__init__.py` and move `places_client.py`

- Create `src/kebi/core/places/__init__.py`
- Move `src/kebi/core/extraction/places_client.py` →
  `src/kebi/core/places/places_client.py`
- Update `core/extraction/validator.py` imports
- Update `api/deps.py` imports (two occurrences of `GooglePlacesClient`)
- Update `GooglePlacesConfig` in config to include `nearbysearch_url: str`

### A5 — Verify A compiles

```bash
poetry run ruff check src/
poetry run mypy src/
poetry run pytest -x
```

---

## Phase B — Protocol Extensions and New Types

### B1 — Extend `PlacesClient` Protocol and `GooglePlacesClient`

Add to `core/places/places_client.py`:

```python
async def discover(
    self, search_location: dict[str, float], filters: dict[str, Any]
) -> list[dict[str, Any]]:
    """Google Places Nearby Search. Returns raw result list."""
    ...

async def validate(
    self, candidate: "Candidate", filters: dict[str, Any]
) -> bool:
    """Validate a candidate against filters (e.g., open now) via Google Places."""
    ...
```

`discover()` calls `nearbysearch_url` with `location={lat},{lng}`, `radius`, and any
supported filter keys (`keyword`, `type`, `opennow`). Falls back gracefully (returns `[]`)
on HTTP error or timeout.

`validate()` calls `nearbysearch_url` with `location={lat},{lng}`, small radius (e.g. 100m),
`keyword=candidate.place_name`, and filters. Returns `True` if any result matches.

### B2 — Create `core/utils/geo.py`

```python
def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two points in metres."""
    ...
```

### B3 — Create `core/consult/types.py`

- `Candidate` Pydantic model (fields per data-model.md)
- `CandidateMapper` Protocol with `map()` method
- `RecallResultToCandidateMapper` implementing `CandidateMapper`
- `ExternalCandidateMapper` implementing `CandidateMapper`

### B4 — Verify B compiles

```bash
poetry run ruff check src/
poetry run mypy src/
```

---

## Phase C — Schema Fixes

### C1 — Add `lat`/`lng` to `RecallRow` and `RecallResult`

- `RecallRow` TypedDict in `db/repositories/recall_repository.py`:
  add `lat: float | None` and `lng: float | None`
- Both SQL queries (`_hybrid_vector_text_search` and `_text_only_search`):
  add `p.lat, p.lng` to the `SELECT` clause
- `RecallResult` in `api/schemas/recall.py`:
  add `lat: float | None = None` and `lng: float | None = None`
- `RecallService.run()` in `core/recall/service.py`:
  populate `lat=row["lat"], lng=row["lng"]` when constructing `RecallResult`

### C2 — Fix `PlaceResult.photos` and remove `stream`

- `api/schemas/consult.py`:
  - `photos: list[str] = Field(min_length=1)` → `photos: list[str] = []`
  - Remove `stream: bool = False` from `ConsultRequest`

### C3 — Verify C compiles and tests pass

```bash
poetry run pytest tests/api/ tests/core/recall/ -x
poetry run mypy src/
```

---

## Phase D — Intent Parser Update

### D1 — Add 3 fields to `ParsedIntent`

```python
validate_candidates: bool = False
discovery_filters: dict[str, Any] = {}
search_location: dict[str, float] | None = None
```

### D2 — Update `IntentParser` system prompt

Inject `radius_defaults` from config at startup. The system prompt must instruct the LLM:

1. Return `radius` as an integer in metres. Reference values injected from config:
   "nearby" / proximity signals → {nearby}m, "walking distance" → {walking}m. Default if
   no proximity signal: null (caller will use {default}m fallback).
2. Set `validate_candidates` to true only when the query implies a live constraint
   (e.g., "open now", "open tonight", "currently open").
3. Populate `discovery_filters` with relevant Google Places filter keys derived from
   the query (e.g., `{"opennow": True}`, `{"type": "restaurant"}`, `{"keyword": "sushi"}`).
4. Set `search_location` to a `{"lat": ..., "lng": ...}` dict resolved from:
   - Request lat/lng — when query implies current location ("nearby", "near me")
   - Geocoded city/neighbourhood — when query names a destination ("in Tokyo")
   - Geocoded address — when query contains an address
   - null — only if no location signal and no request location

The system prompt is constructed in `IntentParser.__init__` by reading
`get_config().consult.radius_defaults`. The IntentParser must also accept an optional
`location: dict | None` parameter in `parse()` to pass the request lat/lng to the LLM
as context.

### D3 — Verify D compiles and parser tests pass

```bash
poetry run pytest tests/core/intent/ -x
poetry run mypy src/
```

---

## Phase E — Ranking Update

### E1 — Update `RankingService.rank()` signature

New signature:
```python
def rank(
    self,
    candidates: list[Candidate],
    taste_vector: dict[str, float],
    search_location: dict[str, float] | None,
) -> list[Candidate]:
```

Changes:
- Accept `list[Candidate]` instead of `list[dict]`
- Accept `search_location: dict[str, float] | None`
- If `search_location` is None, override `weights.distance = 0.0` and redistribute
  the weight proportionally to other dimensions (or simply set the distance score to 0.5
  default for all candidates, making distance neutral)
- Compute `distance_score` internally:
  - If candidate has `lat`/`lng` and `search_location` is not None:
    `distance_m = haversine_m(search_location["lat"], search_location["lng"], candidate.lat, candidate.lng)`
    `distance_score = 1.0 / (1.0 + distance_m / 1000.0)`
  - Otherwise: `distance_score = 0.5` (neutral)
- Return `list[Candidate]` sorted by final score descending
- `_compute_taste_similarity` reads `Candidate` fields directly instead of dict `.get()`

### E2 — Verify E compiles and ranking tests pass

```bash
poetry run pytest tests/core/ranking/ -x
poetry run mypy src/
```

---

## Phase F — ConsultService Rewrite

### F1 — Rewrite `core/consult/service.py`

New `__init__`:
```python
def __init__(
    self,
    intent_parser: IntentParser,
    recall_service: RecallService,
    places_client: PlacesClient,
    taste_model_service: TasteModelService,
    ranking_service: RankingService,
) -> None:
```

Remove `_SYSTEM_PROMPT`, `_llm`, and `stream()` entirely.

`async def consult(user_id, query, location) -> ConsultResponse` implements the 6-step pipeline:

**Step 1 — Parse Intent**
```python
intent = await self._intent_parser.parse(
    query,
    location=location.model_dump() if location else None,
)
radius = intent.radius or config.consult.radius_defaults.default
```

**Step 2 — Retrieve Saved Places**
```python
recall_response = await self._recall_service.run(query, user_id)
mapper = RecallResultToCandidateMapper()
saved_candidates: list[Candidate] = []
for result in recall_response.results:
    candidate = mapper.map(result)
    # Post-filter by cuisine, price_range
    if intent.cuisine and candidate.cuisine and intent.cuisine.lower() not in candidate.cuisine.lower():
        continue
    if intent.price_range and candidate.price_range and candidate.price_range != intent.price_range:
        continue
    # Post-filter by distance
    if intent.search_location and candidate.lat is not None and candidate.lng is not None:
        dist_m = haversine_m(intent.search_location["lat"], intent.search_location["lng"],
                             candidate.lat, candidate.lng)
        if dist_m > radius:
            continue
        candidate = candidate.model_copy(update={"distance": dist_m})
    saved_candidates.append(candidate)
```

**Step 3 — Discover External Candidates**
```python
discovered_candidates: list[Candidate] = []
if intent.search_location:
    try:
        raw_results = await self._places_client.discover(
            intent.search_location,
            {**intent.discovery_filters, "radius": radius},
        )
        ext_mapper = ExternalCandidateMapper()
        seen_ids = {c.place_id for c in saved_candidates}
        for raw in raw_results:
            candidate = ext_mapper.map(raw)
            if candidate.place_id not in seen_ids:
                if candidate.lat is not None and candidate.lng is not None:
                    dist_m = haversine_m(intent.search_location["lat"], intent.search_location["lng"],
                                         candidate.lat, candidate.lng)
                    candidate = candidate.model_copy(update={"distance": dist_m})
                discovered_candidates.append(candidate)
    except Exception:
        logger.warning("External discovery failed; continuing with saved candidates only")
```

**Step 4 — Conditional Validation**
```python
validated_saved: list[Candidate] = []
if intent.validate_candidates:
    for candidate in saved_candidates:
        ok = await self._places_client.validate(candidate, intent.discovery_filters)
        if ok:
            validated_saved.append(candidate)
else:
    validated_saved = saved_candidates
```

**Step 5 — Rank**
```python
taste_vector = await self._taste_model_service.get_taste_vector(user_id)
all_candidates = validated_saved + discovered_candidates
ranked = self._ranking_service.rank(all_candidates, taste_vector, intent.search_location)
top3 = ranked[:3]
```

**Step 6 — Build Response**
```python
def _build_reasoning(candidate: Candidate) -> str:
    parts = []
    if candidate.source == "saved":
        parts.append("from your saves")
    if candidate.cuisine:
        parts.append(candidate.cuisine)
    if candidate.price_range:
        parts.append(candidate.price_range)
    if candidate.distance and candidate.distance > 0:
        parts.append(f"{candidate.distance / 1000:.1f} km away")
    if candidate.popularity_score and candidate.popularity_score > 0.7:
        parts.append("highly rated")
    return ", ".join(parts) if parts else "Recommended for you"

place_results = [
    PlaceResult(
        place_name=c.place_name,
        address=c.address,
        reasoning=_build_reasoning(c),
        source=c.source,
        photos=[],
    )
    for c in top3
]

primary = place_results[0] if place_results else _empty_result()
alternatives = place_results[1:]

reasoning_steps = [
    ReasoningStep(step="intent_parsing", summary=f"Parsed: {_intent_summary(intent)}"),
    ReasoningStep(step="retrieval", summary=f"Found {len(saved_candidates)} saved candidates after filters"),
    ReasoningStep(step="discovery", summary=f"Found {len(discovered_candidates)} external candidates"),
    ReasoningStep(step="validation",
                  summary=f"Validated {len(saved_candidates)} saved candidates" if intent.validate_candidates
                          else "Validation skipped"),
    ReasoningStep(step="deduplication", summary=f"Removed duplicates; {len(all_candidates)} total"),
    ReasoningStep(step="ranking", summary=f"Ranked {len(all_candidates)} candidates; top {len(top3)} selected"),
]
```

### F2 — Verify F compiles

```bash
poetry run ruff check src/
poetry run mypy src/
```

---

## Phase G — Route and Deps Wiring

### G1 — Move `get_consult_service()` to `api/deps.py`

```python
def get_consult_service(
    db_session: AsyncSession = Depends(get_session),
    config: AppConfig = Depends(get_config),
) -> ConsultService:
    intent_parser = IntentParser()
    recall_service = RecallService(
        embedder=get_embedder(),
        recall_repo=SQLAlchemyRecallRepository(db_session),
        config=config.recall,
    )
    places_client = GooglePlacesClient()
    taste_model_service = TasteModelService(session=db_session)
    ranking_service = RankingService()
    return ConsultService(
        intent_parser=intent_parser,
        recall_service=recall_service,
        places_client=places_client,
        taste_model_service=taste_model_service,
        ranking_service=ranking_service,
    )
```

### G2 — Clean up `api/routes/consult.py`

- Remove `get_consult_service()` function
- Remove `if body.stream` branch
- Remove `StreamingResponse`, `Request`, `Response` imports
- Import `get_consult_service` from `api.deps`
- Return type: `ConsultResponse` (not `Response`)

```python
@router.post("/consult", response_model=ConsultResponse, status_code=200)
async def consult(
    body: ConsultRequest,
    service: ConsultService = Depends(get_consult_service),
) -> ConsultResponse:
    return await service.consult(body.user_id, body.query, body.location)
```

### G3 — Verify G compiles and full test suite passes

```bash
poetry run pytest -x
poetry run ruff check src/ tests/
poetry run mypy src/
```

---

## Phase H — Tests

### H1 — `tests/core/consult/test_types.py`

- `RecallResultToCandidateMapper.map()` → source="saved", correct field mapping
- `ExternalCandidateMapper.map()` → source="discovered", price_level mapping, popularity normalisation
- Deduplication: mapper chain correctly removes duplicate place_id

### H2 — `tests/core/consult/test_service.py`

- Step 1 intent parsing failure → `ValueError` propagates (tested via mock)
- Step 3 external provider failure → graceful fallback to saved candidates only
- Deduplication: place in saved + discovered → only saved version in result
- No saved candidates + discovery returns results → source="discovered"
- `validate_candidates=True` → validate() called for each saved candidate; failed ones excluded
- `validate_candidates=False` → validate() never called
- `search_location=None` → distance filtering skipped; ranking called with `search_location=None`
- Zero taste interactions → DEFAULT_VECTOR used; ranking falls back to taste-neutral scoring

### H3 — `tests/core/places/test_places_client.py`

- `discover()` builds correct URL params and returns mapped list
- `discover()` on HTTP error returns `[]` (graceful fallback)
- `validate()` returns `True` when nearby result found, `False` otherwise

### H4 — `tests/core/ranking/test_service.py` (update)

- `rank()` with `list[Candidate]` and `search_location` → correct distance scoring
- `rank()` with `search_location=None` → distance score neutral (0.5), no error
- Deterministic ordering for same inputs

### H5 — `tests/core/intent/test_intent_parser.py` (update)

- `ParsedIntent` contains `validate_candidates`, `discovery_filters`, `search_location`
- Mocked Instructor response with all 6 fields passes Pydantic validation

### H6 — `tests/api/test_consult.py` (update)

- `stream` field rejected as unexpected (400/422)
- Response contains no `photos` validation error with empty list
- `primary` + `alternatives` structure present

---

## Phase I — Final Verification

```bash
poetry run pytest
poetry run ruff check src/ tests/
poetry run mypy src/
```

All must pass before marking complete.

---

## Verify Commands (summary)

```bash
poetry run pytest                         # all tests
poetry run pytest tests/core/consult/ -v  # consult-specific
poetry run ruff check src/ tests/
poetry run mypy src/
```

## Deferred

- SSE streaming + LangGraph StateGraph → Phase 4 (ADR-050)
- Parallel Step 2/3 branches → Phase 4 (ADR-009)
- Full place type coverage beyond food/cuisine → Phase 6
