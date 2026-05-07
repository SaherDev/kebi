# Tasks: Consult Pipeline

**Input**: Design documents from `specs/015-consult-pipeline/`
**Branch**: `015-consult-pipeline`
**Tests**: Included as verification tasks after each phase (not TDD — no explicit request in spec).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no shared state)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)
- Paths are relative to `src/kebi/` unless prefixed otherwise

---

## Phase 1: Setup — ADRs

**Purpose**: Record the two architectural decisions that unblock all implementation.
Both existing ADRs (ADR-021, ADR-022) are superseded here. No code compiles cleanly
against the old locations/assumptions until these are written.

- [ ] T001 Add ADR-049 (supersedes ADR-022) to `docs/decisions.md`: PlacesClient moved from `core/extraction/` to `core/places/`; rationale: shared module not owned by either pipeline
- [ ] T002 Add ADR-050 (supersedes ADR-021) to `docs/decisions.md`: LangGraph deferred to Phase 4; ConsultService is a plain sequential async class for this phase

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Infrastructure changes that every user story depends on. No story work begins until this phase is complete.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

### Config

- [ ] T003 [P] Add `consult.radius_defaults` block (default: 2000, nearby: 1000, walking: 500) and `external_services.google_places.nearbysearch_url` to `config/app.yaml`
- [ ] T004 [P] Add `RadiusDefaultsConfig` Pydantic model and `radius_defaults: RadiusDefaultsConfig` field to `ConsultConfig`; add `nearbysearch_url: str` to `GooglePlacesConfig` in `src/kebi/core/config.py`

### PlacesClient relocation (C2)

- [ ] T005 Create `src/kebi/core/places/__init__.py`
- [ ] T006 Move `src/kebi/core/extraction/places_client.py` → `src/kebi/core/places/places_client.py` (file move only — no logic changes yet)
- [ ] T007 [P] Update `from kebi.core.extraction.places_client import GooglePlacesClient` → `core.places` in `src/kebi/core/extraction/validator.py`
- [ ] T008 [P] Update both occurrences of `from kebi.core.extraction.places_client import GooglePlacesClient` → `core.places` in `src/kebi/api/deps.py`

### RecallResult lat/lng (C8)

- [ ] T009 Add `lat: float | None` and `lng: float | None` to `RecallRow` TypedDict; add `p.lat, p.lng` to `SELECT` in both `_hybrid_vector_text_search()` and `_text_only_search()` in `src/kebi/db/repositories/recall_repository.py`
- [ ] T010 [P] Add `lat: float | None = None` and `lng: float | None = None` to `RecallResult` in `src/kebi/api/schemas/recall.py`
- [ ] T011 Update `RecallService.run()` to populate `lat=row["lat"], lng=row["lng"]` when constructing each `RecallResult` in `src/kebi/core/recall/service.py`

### Schema fixes (C6, C9)

- [ ] T012 [P] In `src/kebi/api/schemas/consult.py`: remove `stream: bool = False` from `ConsultRequest`; change `photos: list[str] = Field(min_length=1)` → `photos: list[str] = []` on `PlaceResult`

### Utilities and new types

- [ ] T013 [P] Create `src/kebi/core/utils/geo.py` with `haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float` using the haversine formula (returns distance in metres)
- [ ] T014 Create `src/kebi/core/consult/types.py` with: `Candidate` Pydantic model (all fields per data-model.md); `CandidateMapper` Protocol with `map()` method; `RecallResultToCandidateMapper` (source="saved", maps RecallResult fields); `ExternalCandidateMapper` (source="discovered", maps Google raw dict, normalises price_level and popularity_score)

**Checkpoint**: Run `poetry run ruff check src/ && poetry run mypy src/` — must pass before Phase 3.

---

## Phase 3: User Story 1 — Recommendations from Saved Places (Priority: P1) 🎯 MVP

**Goal**: A user with saved places queries the system and receives ranked recommendations from their history, with real intent parsing, distance filtering, and taste-profile ranking.

**Independent Test**: `POST /v1/consult` with a `user_id` that has saved places and a `location` → response contains `primary` with `source="saved"`, non-empty `reasoning`, and non-empty `reasoning_steps`.

### ParsedIntent update (C3)

- [ ] T015 [P] [US1] Add `validate_candidates: bool = False`, `discovery_filters: dict[str, Any] = {}`, and `search_location: dict[str, float] | None = None` to `ParsedIntent` in `src/kebi/core/intent/intent_parser.py`
- [ ] T016 [US1] Update `IntentParser.__init__` to read `get_config().consult.radius_defaults` and build the system prompt string at startup, injecting the three defaults; update `parse(query, location: dict | None = None)` to include request location in the LLM messages as context in `src/kebi/core/intent/intent_parser.py`

### RankingService update (C4)

- [ ] T017 [P] [US1] Update `RankingService.rank()` signature to `rank(candidates: list[Candidate], taste_vector: dict[str, float], search_location: dict[str, float] | None) -> list[Candidate]` in `src/kebi/core/ranking/service.py`
- [ ] T018 [US1] Add `_compute_distance_score(candidate: Candidate, search_location: dict | None) -> float` to `RankingService` using `haversine_m()`; set distance score to 0.5 (neutral) when `search_location is None` or candidate has no lat/lng; update `rank()` to use it and return `list[Candidate]` in `src/kebi/core/ranking/service.py`
- [ ] T019 [US1] Update `RankingService._compute_taste_similarity()` and `_get_place_observation()` to read from `Candidate` fields directly (`.cuisine`, `.price_range`, etc.) instead of `dict.get()` in `src/kebi/core/ranking/service.py`

### ConsultService core rewrite (C5, C6)

- [ ] T020 [US1] Rewrite `ConsultService.__init__` in `src/kebi/core/consult/service.py` to accept `intent_parser: IntentParser`, `recall_service: RecallService`, `places_client: PlacesClient`, `taste_model_service: TasteModelService`, `ranking_service: RankingService`; remove `_llm`, `_SYSTEM_PROMPT`, and `stream()` method entirely
- [ ] T021 [US1] Implement Step 1 (parse intent, apply radius fallback from config) and Step 2 (recall, post-filter by cuisine/price_range, post-filter by distance using `haversine_m`, map to `Candidate` via `RecallResultToCandidateMapper`) in `ConsultService.consult()` in `src/kebi/core/consult/service.py`; Steps 3 and 4 are no-op stubs returning empty list / passthrough for this story
- [ ] T022 [US1] Implement Step 5 (get taste vector, call `ranking_service.rank()`) and Step 6 (map top-3 to `PlaceResult` with deterministic reasoning string from candidate data fields, build `reasoning_steps` from actual step results) in `ConsultService.consult()` in `src/kebi/core/consult/service.py`
- [ ] T023 [US1] Add HTTP 500 error propagation for intent parser failure in `ConsultService.consult()` (let exception propagate — FastAPI maps to 500 per ADR-023) in `src/kebi/core/consult/service.py`

### Wiring (C7)

- [ ] T024 [US1] Add `get_consult_service(db_session: AsyncSession = Depends(get_session), config: AppConfig = Depends(get_config)) -> ConsultService` to `src/kebi/api/deps.py`, wiring all 5 dependencies
- [ ] T025 [US1] Remove `get_consult_service()` and `if body.stream` branch from `src/kebi/api/routes/consult.py`; remove `StreamingResponse`, `Request`, `Response` imports; import `get_consult_service` from `api.deps`; return type `ConsultResponse` directly

### Verification tests for US1

- [ ] T026 [P] [US1] Write tests for `RecallResultToCandidateMapper.map()` (source="saved", field mapping, distance=0.0) in `tests/core/consult/test_types.py`
- [ ] T027 [P] [US1] Write tests for `RankingService.rank()` with `list[Candidate]` and `search_location` (distance scoring, neutral scoring when None, deterministic order) in `tests/core/ranking/test_service.py`
- [ ] T028 [US1] Write tests for `ConsultService.consult()` US1 flow: saved places returned, search_location=None skips distance filter, intent parser exception → propagates in `tests/core/consult/test_service.py`

**Checkpoint**: `POST /v1/consult` returns saved place recommendations with source="saved". `poetry run pytest tests/core/consult/ tests/core/ranking/ -x` passes.

---

## Phase 4: User Story 2 — External Discovery (Priority: P2)

**Goal**: When saved places are absent or insufficient, the system discovers external candidates via Google Places Nearby Search and returns them ranked alongside saved results.

**Independent Test**: `POST /v1/consult` with a `user_id` that has no saved places in the queried location but a valid `location` → response contains `primary` with `source="discovered"`.

### PlacesClient discover() (C1)

- [ ] T029 [P] [US2] Add `async def discover(self, search_location: dict[str, float], filters: dict[str, Any]) -> list[dict[str, Any]]` to `PlacesClient` Protocol in `src/kebi/core/places/places_client.py`
- [ ] T030 [US2] Implement `GooglePlacesClient.discover()` in `src/kebi/core/places/places_client.py`: call `nearbysearch_url` with `location={lat},{lng}`, `radius`, and any supported filter keys (`keyword`, `type`, `opennow`); return `results` list from response; return `[]` on any `httpx.HTTPError` or `httpx.TimeoutException` (graceful fallback, logs warning)

### ConsultService Step 3

- [ ] T031 [US2] Implement Step 3 in `ConsultService.consult()` in `src/kebi/core/consult/service.py`: call `places_client.discover(intent.search_location, {**intent.discovery_filters, "radius": radius})` when `search_location` is not None; map results via `ExternalCandidateMapper`; compute distance on each discovered candidate using `haversine_m`; deduplicate against saved candidates by `place_id` (saved entry wins); wrap in try/except for graceful fallback

### Verification tests for US2

- [ ] T032 [P] [US2] Write tests for `ExternalCandidateMapper.map()` (source="discovered", price_level mapping, popularity normalisation) in `tests/core/consult/test_types.py`
- [ ] T033 [P] [US2] Write tests for `GooglePlacesClient.discover()`: correct URL params built, returns mapped list on success, returns `[]` on HTTP error in `tests/core/places/test_places_client.py`
- [ ] T034 [US2] Write tests for `ConsultService` Step 3: external discovery used when saved places absent; deduplication removes discovered entry when same place_id exists in saved set; provider failure → saved-only result returned in `tests/core/consult/test_service.py`

**Checkpoint**: `POST /v1/consult` with no saved places returns discovered results. `poetry run pytest tests/core/consult/ tests/core/places/ -x` passes.

---

## Phase 5: User Story 3 — Open Now Validation (Priority: P3)

**Goal**: When a query signals live constraints ("open now"), saved candidates are individually validated via the places provider; those failing validation are excluded before ranking.

**Independent Test**: `POST /v1/consult` with query containing "open now" → validated saved candidates only; `validate_candidates=True` in parsed intent; closed places excluded.

### PlacesClient validate() (C1)

- [ ] T035 [P] [US3] Add `async def validate(self, candidate: Candidate, filters: dict[str, Any]) -> bool` to `PlacesClient` Protocol in `src/kebi/core/places/places_client.py`
- [ ] T036 [US3] Implement `GooglePlacesClient.validate()` in `src/kebi/core/places/places_client.py`: call `nearbysearch_url` with `location={candidate.lat},{candidate.lng}`, `radius=100`, `keyword=candidate.place_name`, and relevant filter keys (e.g. `opennow`); return `True` if any result is returned, `False` otherwise; return `True` on HTTP error (fail open — don't drop candidates due to API issues)

### ConsultService Step 4

- [ ] T037 [US3] Implement Step 4 in `ConsultService.consult()` in `src/kebi/core/consult/service.py`: if `intent.validate_candidates` is True, iterate saved candidates, call `places_client.validate(candidate, intent.discovery_filters)`, keep only those returning True; if False, passthrough unchanged; update `reasoning_steps` to reflect validation outcome

### Verification tests for US3

- [ ] T038 [P] [US3] Write tests for `GooglePlacesClient.validate()`: returns True when nearbysearch finds a match, False when no match, True on HTTP error in `tests/core/places/test_places_client.py`
- [ ] T039 [US3] Write tests for `ConsultService` Step 4: validate_candidates=True → validate() called per saved candidate, failing ones excluded; validate_candidates=False → validate() never called in `tests/core/consult/test_service.py`

**Checkpoint**: All three user stories independently testable. `poetry run pytest tests/core/consult/ tests/core/places/ -x` passes.

---

## Phase 6: Polish & Cross-Cutting Verification

**Purpose**: Complete test coverage for utilities and integration; final quality gate.

- [ ] T040 [P] Write unit tests for `haversine_m()` (known distances, zero distance, antipodal points) in `tests/core/utils/test_geo.py`
- [ ] T041 [P] Update `tests/core/intent/test_intent_parser.py`: verify `ParsedIntent` accepts and validates all 6 fields including `validate_candidates`, `discovery_filters`, `search_location`
- [ ] T042 [P] Update `tests/api/test_consult.py`: verify `stream` field is rejected, `photos=[]` passes validation, response shape matches updated contract
- [ ] T043 Run full verification suite: `poetry run pytest` (all pass), `poetry run ruff check src/ tests/` (no violations), `poetry run mypy src/` (no errors)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1 — **BLOCKS all user stories**
- **Phase 3 (US1)**: Depends on Phase 2 — no dependency on US2/US3
- **Phase 4 (US2)**: Depends on Phase 2 — depends on Phase 3 (ConsultService skeleton must exist)
- **Phase 5 (US3)**: Depends on Phase 2 — depends on Phase 4 (PlacesClient Protocol must have discover())
- **Phase 6 (Polish)**: Depends on Phases 3, 4, 5 complete

### Within Phase 2

T003, T004 → parallel (different files)
T005 → T006 (must create `__init__.py` first)
T007, T008 → parallel after T006 (different files)
T009 → T010, T011 (T010 is different file, can go parallel with T009; T011 depends on T009)
T012, T013, T014 → parallel (different files)

### Within Phase 3

T015, T017 → parallel (different files)
T016 → after T015; T018, T019 → after T017
T020 → after T015/T016 done (imports ParsedIntent)
T021, T022, T023 → sequential (same method in same file)
T024 → after T020; T025 → after T024
T026, T027 → parallel (different test files); T028 → after T020-T023

### User Story Dependencies

- **US1 (P1)**: After Phase 2 — no US2/US3 dependency
- **US2 (P2)**: After Phase 2 + US1 (ConsultService.consult() stub must exist for Step 3)
- **US3 (P3)**: After Phase 2 + US2 (PlacesClient Protocol should have discover() before validate())

---

## Parallel Example: Phase 2

```bash
# Can run in parallel (different files):
Task T003: "Add radius_defaults + nearbysearch_url to config/app.yaml"
Task T004: "Add RadiusDefaultsConfig to ConsultConfig in core/config.py"
Task T012: "Fix ConsultRequest.stream + PlaceResult.photos in api/schemas/consult.py"
Task T013: "Create core/utils/geo.py with haversine_m()"
```

## Parallel Example: Phase 3 US1

```bash
# Can run in parallel after T006:
Task T007: "Update validator.py import"
Task T008: "Update deps.py imports"

# Can run in parallel (different files):
Task T015: "Add 3 fields to ParsedIntent"
Task T017: "Update RankingService.rank() signature"

# Can run in parallel (different test files):
Task T026: "tests for RecallResultToCandidateMapper"
Task T027: "tests for RankingService with Candidate"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1 (ADRs)
2. Complete Phase 2 (Foundational)
3. Complete Phase 3 (US1 — saved places)
4. **STOP and VALIDATE**: `POST /v1/consult` returns ranked saved places
5. Proceed to US2 only after US1 verified end-to-end

### Incremental Delivery

1. Phase 1 + 2 → infrastructure ready
2. Phase 3 (US1) → saved recommendations working → **deployable MVP**
3. Phase 4 (US2) → external discovery added → richer results for new users
4. Phase 5 (US3) → live validation added → time-sensitive queries handled
5. Phase 6 → full test suite green, mypy clean

---

## Notes

- Conflicts C1–C10 map to tasks as: C1→T029/T035, C2→T005-T008, C3→T003/T004/T015/T016, C4→T017-T019, C5→T020, C6→T012/T025, C7→T024/T025, C8→T009-T011, C9→T012, C10→T003/T004
- `ConsultService.consult()` is built incrementally: US1 (T020-T023) adds skeleton + Steps 1,2,5,6; US2 (T031) adds Step 3; US3 (T037) adds Step 4
- No Alembic migration needed — `places.lat` and `places.lng` columns already exist
- `GooglePlacesClient.validate()` fails open (returns True on HTTP error) to avoid dropping candidates due to transient API issues
- Commit after each checkpoint, not after each individual task
