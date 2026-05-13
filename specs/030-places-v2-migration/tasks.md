---

description: "Tasks for extraction → places_v2 cutover (030-places-v2-migration)"
---

# Tasks: Extraction → Places v2 Cutover

**Input**: Design documents from `/specs/030-places-v2-migration/`
**Prerequisites**: plan.md, spec.md (user stories), research.md (R-01..R-14), data-model.md, contracts/http-response-parity.md, quickstart.md

**Tests**: Required by the spec (FR-015 mandates extraction test rewrites; SC-005/006/007 require a parity test). Test tasks are included inline.

**Organization**: Tasks grouped by user story (US1 / US2 / US3) per spec.md priority ordering. Foundational structural edits live in Phase 2 because the LLM picker and persistence rewrite in US1 cannot serialize without the v2 schema swap, so the structural sweep is a hard prerequisite for every story.

## Format: `[ID] [P?] [Story?] Description`

- **[P]**: Different file, no dependency on an incomplete task → can run in parallel.
- **[Story]**: US1 / US2 / US3 maps to spec.md user stories.
- File paths are repo-relative.

## Path Conventions

- Source code under `src/kebi/`
- Tests under `tests/` mirroring `src/kebi/`
- This feature touches `src/kebi/core/extraction/`, `src/kebi/api/{routes,schemas,deps}`, `src/kebi/core/agent/tools/save_tool.py` and matching `tests/`
- `src/kebi/core/places_v2/` is **FROZEN** (FR-010a) — no task may modify any file under that directory

---

## Phase 1: Setup (Pre-cutover baseline capture)

**Purpose**: Capture the current behavior on a fixed fixture set so the parity test in US3 has something to compare against. Must run on `dev` baseline (the pre-cutover code), not on this feature branch's WIP.

- [X] T001 Create the fixture input set in `tests/core/extraction/fixtures/v2_cutover/inputs.json` with ~20 representative extraction inputs (mix: TikTok video URL, TikTok carousel URL, Instagram post URL, YouTube short URL, Google Maps shared-list URL, manual text with place name, manual text with no recognizable place, multi-place caption)
- [ ] T002 [P] Capture partition-count baseline: check out `dev`, run `poetry run python scripts/v2_cutover_baseline_partition.py` (script: for each fixture, run `ExtractionService.run`, dump `(saved_count, needs_review_count, dropped_count)` keyed by fixture name to `tests/core/extraction/fixtures/v2_cutover/baselines/partition_counts.json`)
- [ ] T003 [P] Capture latency baseline: same script as T002 records wall-clock per fixture and p95 across the set into `tests/core/extraction/fixtures/v2_cutover/baselines/latency_ms.json`
- [ ] T004 [P] Capture response-envelope baseline: for each fixture, serialize the legacy `ExtractPlaceResponse` to JSON and write to `tests/core/extraction/fixtures/v2_cutover/baselines/response_envelopes/<fixture_id>.json`

**Checkpoint**: Baselines committed on the feature branch. Now safe to start mutating extraction code.

---

## Phase 2: Foundational (Structural Sweep — Blocks All User Stories)

**Purpose**: The structural file edits that every behavioral story depends on. Types, mappers, filters, dependency wiring, response schema swap. After Phase 2 the codebase compiles against v2 types but behavior may still reference the old flow until US1 lands.

**⚠️ CRITICAL**: No US1/US2/US3 task may begin until this phase is complete.

- [ ] T005 [P] Rewrite `src/kebi/core/extraction/types.py`: drop every legacy `core.places` re-export (`PlaceAttributes`, `PlaceCreate`, `PlaceObject`, `PlaceProvider`, `PlacesMatchQuality`, `PlaceSource`, `PlaceType`); delete `SearchMatch`; reshape `ValidatedCandidate` to (`place_name`, `provider_id`, `categories: list[PlaceCategory]`, `tags: list[PlaceTag]`, `confidence`, `evidence`, `subcategory`, `location: LocationContext | None`); change `ExtractionContext.source` to v2 `PlaceSource`; remove `search_matches` field from `ExtractionContext`; keep `Producer`, `Medium`, `EvidenceField`, `Evidence`, `KnownPlace` unchanged
- [X] T006 [P] Create `src/kebi/core/extraction/geo_filter.py` exposing `drop_geographic_features(places: list[PlaceObject]) -> list[PlaceObject]` — filters out administrative-only results (locality, country, route) by `PlaceCategory` and name heuristic (small allow-list of administrative noise patterns at the top of the file)
- [X] T007 [P] Create `src/kebi/core/extraction/candidate_mapper.py` with two functions: `candidate_to_core(c: ValidatedCandidate) -> PlaceCore` (builds a v2 `PlaceCore` from the picker output) and `search_results_to_picker_input(places: list[PlaceObject]) -> list[<PickerCandidate>]` (projects v2 `PlaceObject` into the slim shape the picker prompt consumes — name, provider_id, location summary)
- [X] T008 [P] Update `src/kebi/core/extraction/url_source.py`: change `PlaceSource` import to `from kebi.core.places_v2 import PlaceSource`; verify every enum value referenced still exists on v2 enum (`tiktok`, `instagram`, `youtube`, `google_maps_list`, `manual`)
- [X] T009 [P] Update `src/kebi/core/extraction/source_filtered_enricher.py`: change `PlaceSource` import to v2; verify `allowed_sources` declarations on enricher subclasses still resolve
- [ ] T010 Update `src/kebi/core/extraction/dedup.py`: replace existing function(s) with a single `dedup_by_provider_id(candidates: list[ValidatedCandidate]) -> list[ValidatedCandidate]` that keeps the highest-confidence candidate per `provider_id`; drop legacy imports (`PlaceAttributes`, `build_provider_id`) — depends on T005
- [ ] T011 Wire v2 services in `src/kebi/api/deps.py` for the extraction surface: add `get_places_search_service`, `get_place_upsert_service`, `get_user_places_service`, `get_user_places_repo` factories that compose `PlacesRepo`, `RedisPlacesCache`, `GooglePlacesClient`, `EmbeddingService`, `EmbeddingsRepo`, `UserPlacesRepo` from `core/places_v2/`; leave legacy factories (`get_places_service`, `get_places_cache`, `get_places_repository`) untouched — recall/consult still depend on them
- [ ] T012 [P] Swap response-schema import in `src/kebi/api/schemas/extract_place.py`: `ExtractPlaceItem.place` becomes v2 `PlaceObject`; `ExtractPlaceItem.status` literal stays `"saved" | "needs_review" | "duplicate"` (back-compat per ADR-071)
- [ ] T013 [P] Update `src/kebi/core/agent/tools/save_tool.py`: the extraction return value the tool wraps is now a v2 `ExtractPlaceItem` shape; update any field accesses (`.place.place_type` → `.place.categories[0]` or summary string; `.place.attributes` → `.place.tags`); keep the tool's user-visible summary string format reasonable for the agent SSE feed

**Checkpoint**: `poetry run mypy src/kebi/core/extraction src/kebi/api/schemas/extract_place.py src/kebi/api/deps.py src/kebi/core/agent/tools/save_tool.py` should pass except for downstream call sites that haven't been updated yet (US1 fixes those).

---

## Phase 3: User Story 1 — Extraction persists saves only to the v2 store (Priority: P1) 🎯 MVP

**Goal**: Every place row, geo cache entry, and embedding produced by extraction lands in v2. Legacy places/embeddings tables and legacy Redis namespaces receive zero new writes from extraction. `ExtractionPersistenceService` deleted; service-level orchestration uses v2 services directly (ADR-070, ADR-071).

**Independent Test**: Run an end-to-end extraction through `POST /v1/extract` or `POST /v1/chat`. Verify: (a) new row in `places_v2` table; (b) zero new rows in legacy `places`; (c) new row in `place_embeddings_v2`; (d) cache entries only under v2 namespace; (e) new `user_places` row with `approved=False`; (f) response envelope returns successfully.

### Implementation for User Story 1

- [ ] T014 [US1] Rewrite the Instructor output Pydantic model in `src/kebi/core/extraction/enrichers/llm_picker.py`: drop `place_type: PlaceType`, `provider: PlaceProvider`, `external_id: str`, `attributes: PlaceAttributes`; emit `categories: list[PlaceCategory]` (1–3 per pick, ordered most-specific first), `tags: list[PlaceTag]` (each stamped `source="llm"`), `provider_id: str` (must be namespaced); keep `confidence`, `evidence_fields`, `subcategory`, `place_name`
- [ ] T015 [US1] Rewrite picker prompt in `src/kebi/core/extraction/enrichers/llm_picker.py` to describe v2 vocabulary: enumerate the allowed `PlaceCategory` values, list `TagType` axes with their enum value sets, instruct the LLM to pick by `provider_id` (echoing the search-result set rather than emitting a bare external id)
- [ ] T016 [US1] Update `reconcile_picks` in `src/kebi/core/extraction/enrichers/llm_picker.py` to join picker output against the search-service result set by `provider_id` (was: by `(provider, external_id)`); drop picks whose `provider_id` is not in the search set (preserves safety-net behavior); simplify the "echoed fields win" rule because `PlacesSearchService` results are already authoritative
- [X] T017 [P] [US1] Update `src/kebi/core/extraction/enrichers/instagram_post.py`: change `PlaceSource` import to v2
- [X] T018 [P] [US1] Update `src/kebi/core/extraction/enrichers/google_maps_list.py`: change `PlaceSource` import to v2 (and the `.google_maps` value reference → `.google_maps_list`)
- [X] T019 [P] [US1] Update `src/kebi/core/extraction/enrichers/tiktok_caption.py`: change `PlaceSource` import to v2
- [X] T020 [P] [US1] Update `src/kebi/core/extraction/enrichers/tiktok_photo.py`: change `PlaceSource` import to v2
- [X] T021 [P] [US1] Update `src/kebi/core/extraction/enrichers/video_metadata.py`: change `PlaceSource` import to v2
- [X] T022 [P] [US1] Update `src/kebi/core/extraction/enrichers/vision_images.py`: change `PlaceSource` import to v2
- [ ] T023 [US1] Update `src/kebi/core/extraction/extraction_pipeline.py`: replace inline `PlacesSearcher.search(...)` calls with `PlacesSearchService.find(query)` (one call per unique candidate name, returning `list[PlaceObject]`); apply `geo_filter.drop_geographic_features` to the result before passing to the picker; preserve the per-level `summary_fn` and emit step names (`save.enrich`, `save.deep_enrichment`); keep `_enforce_candidate_limit` (`TooManyCandidatesError` semantics unchanged)
- [ ] T024 [US1] Delete `src/kebi/core/extraction/searcher.py` (the legacy `PlacesSearcher` and `SearchMatch`-producing search step — ADR-070)
- [ ] T025 [US1] Delete `src/kebi/core/extraction/persistence.py` (the legacy `ExtractionPersistenceService` — ADR-071; behavior moves inline into `service.py`)
- [ ] T026 [US1] Rewrite `ExtractionService.run` in `src/kebi/core/extraction/service.py` to inline the v2 persistence flow: after the picker → dedup → geo-filter step, build `cores = [candidate_to_core(c) for c in candidates]`, call `await self._upsert.upsert_and_embed(cores)` to get persisted `PlaceCore`s with ids, then `existing_ids = await self._user_places_repo.get_existing_place_ids(user_id, [p.id for p in persisted])`, split into `to_link` and `duplicates`, call `await self._user_places.save_places(user_id=ctx.user_id, places=to_link, source=ctx.source, source_url=ctx.url)`, then dispatch one `PlaceSaved` event per linked place; build response with each `to_link` as `status="saved"` and each `duplicates` as `status="duplicate"`; remove all references to confidence partitioning
- [ ] T027 [US1] Rewrite `tests/core/extraction/test_service.py` against v2 stubs: cover the inline persistence flow (upsert_and_embed called once, save_places called with non-duplicates, dispatch called per linked place); assert duplicates surface as `status="duplicate"`; assert no `needs_review` status is ever emitted
- [ ] T028 [US1] Rewrite `tests/core/extraction/test_extraction_pipeline.py`: cover the new search delegation (`PlacesSearchService.find` stubbed); cover `geo_filter` removes administrative-only places; cover `_enforce_candidate_limit` unchanged
- [ ] T029 [US1] Rewrite `tests/core/extraction/test_dedup.py` against the new `dedup_by_provider_id` function with the reshaped `ValidatedCandidate`
- [ ] T030 [US1] Rewrite `tests/core/extraction/enrichers/test_llm_picker.py`: cover the new Instructor output schema (PlaceCategory list, PlaceTag list, namespaced provider_id); cover prompt-asserted constraints (e.g. picker rejects bare external ids); cover `reconcile_picks` joining by provider_id and dropping picks missing from the search set
- [ ] T031 [US1] Delete `tests/core/extraction/test_persistence.py` (the module it tested is gone; behavioral coverage moved into `test_service.py`)

**Checkpoint**: A live extraction now lands the place + embedding + cache + user_places row only on v2. SC-002/003/004 measurable. Legacy `places` / `embeddings` / `places:*` Redis namespaces receive zero new writes from extraction.

---

## Phase 4: User Story 2 — Extraction internals depend only on v2 types (Priority: P2)

**Goal**: Verify the structural sweep is complete — zero legacy place imports anywhere in `core/extraction/`, type checker happy, legacy-coupled test files removed.

**Independent Test**: `grep -rn "from kebi.core.places import\|from kebi.core.places\." src/kebi/core/extraction/ src/kebi/core/agent/tools/save_tool.py src/kebi/api/routes/extraction.py src/kebi/api/schemas/extract_place.py` returns zero results; `poetry run mypy src/kebi/core/extraction` passes; `tests/core/extraction/test_searcher.py` and `test_places_client.py` no longer exist.

### Implementation for User Story 2

- [ ] T032 [US2] Run `grep -rn "from kebi.core.places import\|from kebi.core.places\." src/kebi/core/extraction/ src/kebi/core/agent/tools/save_tool.py src/kebi/api/routes/extraction.py src/kebi/api/schemas/extract_place.py src/kebi/api/deps.py` and fix any remaining hits by updating imports to `kebi.core.places_v2` (commit fixes to whichever specific file the grep surfaces)
- [ ] T033 [US2] Run `poetry run mypy src/kebi/core/extraction src/kebi/api/schemas/extract_place.py src/kebi/api/routes/extraction.py src/kebi/api/deps.py src/kebi/core/agent/tools/save_tool.py` and fix any leftover legacy type usages
- [ ] T034 [P] [US2] Delete `tests/core/extraction/test_searcher.py` (module deleted in T024)
- [ ] T035 [P] [US2] Delete `tests/core/extraction/test_places_client.py` (legacy `PlacesClient` no longer wired from extraction; v2 client is tested under `tests/core/places_v2/`)
- [ ] T036 [P] [US2] Rewrite `tests/core/extraction/test_types.py`: drop `SearchMatch` tests; cover the reshaped `ValidatedCandidate` (provider_id namespacing, PlaceCategory list, PlaceTag list); cover `ExtractionContext.source` is v2 `PlaceSource`
- [X] T037 [P] [US2] Update `tests/core/extraction/test_url_source.py`: assertions reference v2 `PlaceSource` enum values
- [X] T038 [P] [US2] Update `tests/core/extraction/enrichers/test_google_maps_list.py`: v2 `PlaceSource` import; behavior assertions unchanged

**Checkpoint**: SC-001 measurable (zero legacy imports in extraction). The structural sweep is provably complete.

---

## Phase 5: User Story 3 — Response shape parity (Priority: P3)

**Goal**: Prove the external HTTP response contract is preserved at the field-set level, except for the three documented changes (categories[], tags[], no needs_review emission). Coordinate the breaking changes with the product repo.

**Independent Test**: `poetry run pytest tests/core/extraction/test_v2_cutover_parity.py -v` passes (3 assertions: latency p95 within margin, response field-set parity modulo documented changes, save-count floor satisfied).

### Implementation for User Story 3

- [ ] T039 [US3] Write `tests/core/extraction/test_v2_cutover_parity.py` with three test functions: (a) `test_save_count_floor` — for each fixture in `inputs.json`, run extraction post-cutover and assert `post.saved_count >= baseline.saved_count + baseline.needs_review_count` (per SC-007); (b) `test_latency_p95_parity` — measure p95 across all fixtures and assert `post.p95 <= baseline.p95 * 1.05` (per SC-005); (c) `test_response_field_set_parity` — diff post-cutover serialized envelope against the per-fixture baseline JSON in `tests/core/extraction/fixtures/v2_cutover/baselines/response_envelopes/`, allowing only the three documented changes (`place_type` → `categories`, `attributes` → `tags`, dropped `needs_review` emission); fail on any other key divergence
- [ ] T040 [US3] Run the parity test against current implementation and resolve any unexpected divergence (either fix the implementation or, if the divergence is a genuine and intentional API change not yet documented, update `specs/030-places-v2-migration/contracts/http-response-parity.md` to record it explicitly per FR-012)
- [ ] T041 [US3] Update `kebi-config/bruno/` Bruno collection: revise the `extract-place.bru` (or equivalent) sample response to match the new shape (`categories: string[]`, `tags: Tag[]`, no `needs_review` example response)
- [ ] T042 [US3] Coordinate with the product repo on the three externally observable changes from `contracts/http-response-parity.md` — surface them in the PR description with the exact JSON before/after examples; product-repo TypeScript update must land in the same release window

**Checkpoint**: SC-005, SC-006, SC-007 all green. Externally observable contract changes are explicitly documented and coordinated. US3 ships only after the product repo's matching TypeScript update is also ready.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Full quality gates and quickstart validation.

- [ ] T043 [P] Run `poetry run pytest tests/core/extraction/ -v` and confirm all extraction tests pass
- [ ] T044 [P] Run `poetry run ruff check src/kebi/core/extraction src/kebi/api/schemas/extract_place.py src/kebi/api/routes/extraction.py src/kebi/api/deps.py src/kebi/core/agent/tools/save_tool.py tests/core/extraction/` and confirm no new lint errors
- [ ] T045 [P] Run `poetry run mypy src/kebi/core/extraction src/kebi/api/schemas/extract_place.py src/kebi/api/routes/extraction.py src/kebi/api/deps.py src/kebi/core/agent/tools/save_tool.py` and confirm zero new errors
- [ ] T046 Walk through `specs/030-places-v2-migration/quickstart.md` end to end against a running local instance: confirm steps 2 (no legacy imports), 3 (searcher gone), 4 (extraction tests pass), 5 (live extraction), 6 (DB + Redis state), 7 (response shape), 8 (lint + mypy), 9 (places_v2 untouched), 10 (agent save path) all pass
- [ ] T047 Confirm `git diff dev -- src/kebi/core/places_v2/` is empty (FR-010a invariant — no edits to the frozen v2 module). If any file was accidentally modified, revert it.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — can start immediately. Must complete on the `dev` baseline behavior, not on partially-migrated WIP, because the baselines are pre-cutover.
- **Phase 2 (Foundational)**: Depends on Phase 1 baselines being committed. **Blocks all user stories.**
- **Phase 3 (US1 — P1)**: Depends on Phase 2.
- **Phase 4 (US2 — P2)**: Depends on Phase 3 (because US1 changes touch the same files US2 verifies clean).
- **Phase 5 (US3 — P3)**: Depends on Phase 3 (response envelopes are produced by the new flow).
- **Phase 6 (Polish)**: Depends on Phase 3 + 4 + 5.

### Within Phase 3 (US1)

- T014 → T015 → T016 sequential (same file, picker rewrite happens in three logical chunks)
- T017–T022 parallel (six different enricher files, no inter-dependency)
- T023 (pipeline rewire) depends on T006 (geo_filter) + T007 (candidate_mapper) + T014–T016 (picker emits v2 shape)
- T024 (delete searcher) depends on T023 (pipeline no longer imports it)
- T025 (delete persistence) depends on T026 (service inlines what persistence did)
- T026 (rewrite service.run) depends on T011 (v2 deps) + T007 (mapper) + T023 (pipeline)
- T027–T031 (test rewrites) depend on T026
- T031 (delete test_persistence.py) depends on T025

### Within Phase 5 (US3)

- T039 depends on Phase 1 baselines existing + Phase 3 implementation complete
- T040 depends on T039
- T042 depends on T040 (only coordinate once divergence story is clean)

### Parallel Opportunities

- **Phase 1**: T002 / T003 / T004 can run together (different output files)
- **Phase 2**: T005 / T006 / T007 / T008 / T009 / T012 are all on different files and can run in parallel
- **Phase 3**: T017 / T018 / T019 / T020 / T021 / T022 (six enricher PlaceSource updates) all parallel
- **Phase 4**: T034 / T035 / T036 / T037 / T038 all on different test files, parallel
- **Phase 6**: T043 / T044 / T045 are independent runs

---

## Parallel Example: Phase 2 Foundational Sweep

```bash
# After T001–T004 (baselines) are committed, fan out:
Task: "T005 Rewrite src/kebi/core/extraction/types.py"
Task: "T006 Create src/kebi/core/extraction/geo_filter.py"
Task: "T007 Create src/kebi/core/extraction/candidate_mapper.py"
Task: "T008 Update src/kebi/core/extraction/url_source.py imports"
Task: "T009 Update src/kebi/core/extraction/source_filtered_enricher.py imports"
Task: "T012 Swap response-schema import in src/kebi/api/schemas/extract_place.py"
```

T010 (dedup.py) and T011 (deps.py) and T013 (save_tool.py) join in sequence — each has a small dependency on one of the parallel ones above.

## Parallel Example: User Story 1 Enricher Updates

```bash
# Once T005 (types.py) lands, fan out the PlaceSource swaps:
Task: "T017 Update core/extraction/enrichers/instagram_post.py PlaceSource import"
Task: "T018 Update core/extraction/enrichers/google_maps_list.py PlaceSource import"
Task: "T019 Update core/extraction/enrichers/tiktok_caption.py PlaceSource import"
Task: "T020 Update core/extraction/enrichers/tiktok_photo.py PlaceSource import"
Task: "T021 Update core/extraction/enrichers/video_metadata.py PlaceSource import"
Task: "T022 Update core/extraction/enrichers/vision_images.py PlaceSource import"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete **Phase 1** (T001–T004) — baselines captured against `dev` head.
2. Complete **Phase 2** (T005–T013) — structural sweep.
3. Complete **Phase 3** (T014–T031) — writes go to v2; persistence service deleted; LLM picker emits v2 shape.
4. **STOP and validate**: end-to-end extraction lands rows in `places_v2`, `place_embeddings_v2`, `user_places`; legacy tables and namespaces receive no new writes. SC-002/003/004 measurable.
5. This is the MVP — extraction is fully on v2.

### Incremental Delivery

1. Setup + Foundational → bench-pressable structural state, behavior unchanged (legacy still wired).
2. Add US1 → live extraction on v2 → MVP demo.
3. Add US2 → grep / mypy gate proven → ship-ready on internal quality bar.
4. Add US3 → parity test green → ship-ready on external contract bar.
5. Polish (Phase 6) → quickstart walk + final gates → merge `030-places-v2-migration` into `dev`.

### Parallel Team Strategy

This is a refactor with high file-level entanglement; not ideal for parallel staffing. Realistic split:

- **Developer A**: Phase 2 + Phase 3 picker / pipeline / service path (T005, T010, T013, T014–T016, T023–T026)
- **Developer B**: Phase 2 + Phase 3 enricher import sweep + tests (T006, T007, T008, T009, T012, T017–T022, T027–T031)
- **Developer C**: Phase 1 baselines + Phase 5 parity test (T001–T004, T039–T042) — can start ahead of everyone else on `dev`

Phase 4 and Phase 6 absorbed by whoever wraps last.

---

## Out of Scope (Reminder)

Per spec 030 and ADR-070 / ADR-071, **NOT** in this task list:

- Modifying any file under `src/kebi/core/places_v2/` (FR-010a)
- Migrating recall, consult, agent recall tool, taste model, user-places API to v2 (separate follow-up feature)
- Deleting the legacy `core/places/` module or dropping legacy DB tables
- Backfilling legacy data into v2
- Retuning embedding model or LLM models

---

## Notes

- `[P]` = different file, no incomplete-task dependency
- Story labels (`[US1]`, `[US2]`, `[US3]`) traceable to spec.md user stories
- Phase 2 (Foundational) and Phase 6 (Polish) carry no story label by spec-kit convention
- Commit after each task or each logical group; the git comment char is `;` not `#` (CLAUDE.md)
- If a task surfaces an unexpected change to `core/places_v2/`, stop and re-confirm — FR-010a forbids it
