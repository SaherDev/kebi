# Implementation Plan: Extraction → Places v2 Cutover

**Branch**: `030-places-v2-migration` | **Date**: 2026-05-13 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/030-places-v2-migration/spec.md`

## Summary

Cut the extraction flow (request parsing → enrichment → search → LLM picker → persistence → embedding → response envelope) over to `core/places_v2/`. All writes (place rows, embeddings, geo cache) land in v2; all extraction internals import v2 types only; the response envelope carries v2 place shape. Read paths (recall, consult, agent recall, taste model, user-places API) are out of scope and continue to use legacy. The v2 module is frozen — every bridging concern lives extraction-side. Per ADR-070, `places_v2.PlacesSearchService` is the single source of truth for place lookups; `core/extraction/searcher.py` is deleted.

## Technical Context

**Language/Version**: Python 3.11
**Primary Dependencies**: FastAPI, LangGraph (agent), LangChain (LLM clients), Pydantic, Instructor (structured LLM output), SQLAlchemy 2 async + asyncpg, redis-py, Voyage AI SDK, OpenAI SDK, Anthropic SDK, Groq SDK, Langfuse
**Storage**: PostgreSQL via SQLAlchemy async (Alembic-managed migrations); Redis for cache + agent state
**Testing**: pytest with `asyncio_mode = "auto"`
**Target Platform**: Linux server (Railway)
**Project Type**: web-service (Python FastAPI; src layout per ADR-001)
**Performance Goals**: end-to-end extraction p95 same or better than pre-migration baseline (SC-005); zero new rows in legacy `places` / legacy `embeddings` / legacy `places:*` cache namespaces during a 24h window after cutover (SC-002/003/004)
**Constraints**: `core/places_v2/` is frozen — no files under that directory are modified by this feature (FR-010a). Externally observed HTTP contracts on `/v1/chat`, `/v1/extract-place`, `/v1/extraction/{request_id}` remain byte-compatible (FR-007, SC-006). Save-count floor preserved on a fixed offline fixture set (post-cutover saves ≥ pre-cutover saved+needs_review, per SC-007 / ADR-071).
**Scale/Scope**: Touches `core/extraction/` (~20 files), `api/routes/extraction.py`, `api/schemas/extract_place.py`, `core/agent/tools/save_tool.py`, extraction tests. Deletes `core/extraction/searcher.py` and `core/extraction/dedup.py`'s legacy-typed bits. Does NOT touch `core/recall/`, `core/consult/`, `core/taste/`, `core/agent/tools/recall_tool.py`, `core/agent/tools/consult_tool.py`, `core/chat/service.py`, `core/places/` (legacy module stays).

## Constitution Check

*Gate before Phase 0 research. Re-checked after Phase 1 design (see end of file).*

| Constitution principle | Status | Notes |
|---|---|---|
| I. Repo boundary | **Pass** | No NestJS contact; no UI; product repo continues to call `POST /v1/chat` and `GET /v1/extraction/{request_id}` only. |
| II. ADRs are constraints | **Pass** | This feature implements ADR-070 (just added). ADR-049 (`PlacesClient` already out of extraction) is built upon. ADR-008 (extract-place is a workflow) unchanged. ADR-054/055 (legacy `PlacesService` strict-create + `search_vector` semantics) are reinterpreted on the v2 schema for the extraction-only scope; legacy services remain in place for read callers and are unaffected. |
| III. Provider abstraction | **Pass** | Embedder, extractor, vision, transcriber roles unchanged. No model names hardcoded in this feature's new code; the LLM picker keeps reading `extractor` from `config/app.yaml`. |
| IV. Pydantic everywhere | **Pass** | The extraction request/response schemas continue to use Pydantic; `ValidatedCandidate` is reshaped against v2 Pydantic types (`PlaceCategory`, `PlaceTag`, v2 `PlaceSource`). No raw dicts at module boundaries. |
| V. Configuration rules | **Pass** | No new config keys; no new secrets. Existing extraction-related keys in `config/app.yaml` (`extraction.*`) continue to apply. |
| VI. DB write ownership | **Pass** | Only AI tables are written: `places_v2`, `place_embeddings_v2`. Legacy `places` / `embeddings` tables are *not* dropped in this feature (out of scope) but no new writes from extraction. |
| VII. Redis ownership | **Pass** | Cache writes shift from legacy `places:geo:*` / `places:enrichment:*` namespaces to whatever v2 cache namespace `core/places_v2/cache.py` already uses. Extraction does not write under legacy prefixes after cutover. |
| VIII. API contract | **Pass** | External response shapes preserved at the field level. Internal type swap (`PlaceObject` legacy → `PlaceObject` v2) is invisible if Pydantic serialization produces the same field set. Validated via fixture comparison (SC-006). |
| IX. Testing | **Pass** | pytest, mypy strict, ruff continue to pass. Tests under `tests/core/extraction/` are updated alongside the source migration. |
| X. Git & commits | **Pass** | Conventional commits, `;` comment char, branch already created from `dev`. |

**Note on superseded ADRs:** Constitution lists ADR-018 (`routes/extract_place.py` separate router) and the older two-endpoint list. Both were superseded by **ADR-052** (consolidation into `routes/chat.py`) and **ADR-048** (status polling endpoint) — both pre-date this feature, no conflict here. The legacy `extract-place` route under the constitution's API Contract is the same flow we're migrating.

**Gate**: All checks pass. No complexity-tracking entries needed.

## Project Structure

### Documentation (this feature)

```text
specs/030-places-v2-migration/
├── plan.md              # This file
├── spec.md              # Feature spec
├── research.md          # Phase 0 — open decisions resolved (mapping tables, picker schema, etc.)
├── data-model.md        # Phase 1 — entities, before/after shapes, conversion rules
├── quickstart.md        # Phase 1 — developer verification steps
├── contracts/
│   └── http-response-parity.md   # Phase 1 — externally observed fields preserved
├── checklists/
│   └── requirements.md  # Spec quality checklist (already created)
└── tasks.md             # Phase 2 output (NOT created by /speckit.plan)
```

### Source Code (repository root) — touched paths

```text
src/kebi/
├── api/
│   ├── routes/
│   │   └── extraction.py           # response wiring (uses v2 PlaceObject; field set preserved)
│   ├── schemas/
│   │   └── extract_place.py        # ExtractPlaceItem.place → v2 PlaceObject
│   └── deps.py                     # extraction-side dependency wiring shifts to v2 services (PlacesSearchService, EmbeddingService, PlaceUpsertService)
├── core/
│   ├── extraction/                 # full migration target
│   │   ├── service.py              # rewired to v2; inlines upsert_and_embed + save_places + dispatch (per ADR-071)
│   │   ├── persistence.py          # DELETED (per ADR-071 — extraction calls v2 services directly from service.py)
│   │   ├── extraction_pipeline.py  # imports + summary fields updated
│   │   ├── searcher.py             # DELETED (per ADR-070)
│   │   ├── dedup.py                # legacy-typed bits replaced; v2 provider_id helper used; small file probably reduces to nearly nothing or merges into pipeline
│   │   ├── types.py                # legacy re-exports DELETED; ValidatedCandidate reshaped; SearchMatch deleted
│   │   ├── url_source.py           # PlaceSource import shifts to v2
│   │   ├── source_filtered_enricher.py  # PlaceSource import shifts to v2
│   │   ├── confidence.py           # unchanged (numeric only, no longer a save gate)
│   │   ├── geo_filter.py           # NEW — thin extraction-side filter dropping geographic-feature matches from PlacesSearchService results (per ADR-070)
│   │   ├── candidate_mapper.py     # NEW — bridges PlacesSearchService PlaceObject → picker input shape, and ValidatedCandidate → v2 PlaceCore for upsert
│   │   └── enrichers/
│   │       ├── instagram_post.py        # PlaceSource v2
│   │       ├── google_maps_list.py      # PlaceSource v2
│   │       ├── llm_picker.py            # output schema rewired to v2 vocab; picker chooses by v2 provider_id; reconcile by provider_id
│   │       ├── tiktok_caption.py        # PlaceSource v2
│   │       ├── tiktok_photo.py          # PlaceSource v2
│   │       ├── video_metadata.py        # PlaceSource v2
│   │       ├── vision_images.py         # PlaceSource v2
│   │       └── (subtitle_check, whisper_audio, vision_frames unchanged — no place-shape contact)
│   └── agent/
│       └── tools/
│           └── save_tool.py        # extraction return type → v2 PlaceObject; agent state recall path NOT touched (legacy-shaped, out of scope)
└── tests/
    └── core/extraction/            # rewritten against v2 services; test_searcher.py DELETED
```

**Untouched (out of scope, intentional):**

```text
src/kebi/core/places/                # legacy module — stays
src/kebi/core/recall/                # legacy reads — stays
src/kebi/core/consult/               # legacy reads — stays
src/kebi/core/chat/service.py        # reads legacy PlacesService for now — stays
src/kebi/core/agent/state.py         # legacy PlaceObject in state — stays (recall path)
src/kebi/core/agent/tools/recall_tool.py     # legacy reads — stays
src/kebi/core/agent/tools/consult_tool.py    # legacy reads — stays
src/kebi/core/taste/                 # uses legacy PlaceAttributes — stays
src/kebi/db/repositories/recall_repository.py  # legacy reads — stays
src/kebi/db/repositories/taste_model_repository.py  # legacy reads — stays
src/kebi/api/routes/user.py          # legacy PlacesService — stays
src/kebi/api/schemas/recall.py       # legacy PlaceObject — stays
src/kebi/api/schemas/consult.py      # legacy PlaceObject — stays
src/kebi/core/places_v2/             # FROZEN — no edits in this feature (FR-010a)
```

**Structure Decision**: Existing src-layout (ADR-001) and hybrid directory (ADR-002) are preserved. Two new files inside `core/extraction/`:
- `geo_filter.py` — thin function dropping geographic-feature matches from `PlacesSearchService` results (the previous searcher's `_drop_geographic_features` lives here).
- `candidate_mapper.py` — extraction-side adapters: v2 `PlaceObject` → picker input projection; picker `ValidatedCandidate` → v2 `PlaceCore` for upsert. Centralizes the legacy↔v2 vocabulary bridge so it does not leak into individual enrichers.

No new top-level packages. No new tests directories. All v2 bridging code lives under `core/extraction/`.

## Phase 0 — Research outputs

See [research.md](./research.md). Resolves:

1. Legacy → v2 type mapping table (`PlaceType` → `PlaceCategory` list, `PlaceAttributes` → `PlaceTag` set, `PlaceProvider` + `external_id` → namespaced `provider_id`, legacy `PlaceSource` → v2 `PlaceSource`, `PlaceCreate` → v2 `PlaceCore`, `PlacesMatchQuality` filter moves into geo-feature filter, `build_provider_id` → v2 namespacing convention `GOOGLE_PROVIDER_PREFIX`).
2. LLM picker output schema redesign (drops `PlaceProvider`/`PlaceType`/`PlaceAttributes` from the Instructor model; emits `PlaceCategory[]` + `PlaceTag[]` + `provider_id`).
3. `reconcile_picks` updates: joins by `provider_id` instead of `(provider, external_id)`; safety-net behavior preserved.
4. Persistence simplification: places already chosen from the `PlacesSearchService` result set are already upserted by the service — extraction's persistence layer focuses only on `ValidatedCandidate → user_places` (if applicable to v2 model), embedding upsert, and emit/response shaping.
5. Embedding flow against v2 `EmbeddingService`/`EmbeddingsRepo` (1024 dims, diff-then-embed).
6. Geo cache writes — eliminated as a separate step; `PlacesSearchService` handles cache mset on the cold path.
7. Test rewrite vs delete decisions (one row per existing test file).
8. Behavioral parity verification: fixture set selection + before/after capture for SC-005/006/007.
9. Cutover rollout: rolling deploy with drain (ADR-070 / clarification Q1 = Option C).
10. ADR-071 supersedes ADR-057 — confidence partition at save time removed; all picker outputs persist as `user_places` rows with `approved=False`; user curates after the fact. `ExtractionPersistenceService` deleted.

## Phase 1 — Design outputs

See:

- [data-model.md](./data-model.md) — entities (Place, Place embedding, Place cache entry, Extraction response envelope, ValidatedCandidate, KnownPlace, ExtractionContext) with before/after shape and conversion rules. Includes the legacy → v2 mapping table at the field level.
- [contracts/http-response-parity.md](./contracts/http-response-parity.md) — externally observed response shapes on `POST /v1/chat`, `POST /v1/extract-place`, `GET /v1/extraction/{request_id}` with byte-level field invariants and the test approach to enforce them.
- [quickstart.md](./quickstart.md) — developer steps to verify the cutover locally: install, run tests, hit endpoints, inspect DB + cache.

Agent context script: invoked by Phase 1 — see `CLAUDE.md` recent-changes section for the appended entry.

## Re-evaluation of Constitution Check after Phase 1

Re-checked after writing research + data-model + contracts. **No new violations.**

- Principle I (repo boundary): no change — feature stays inside the AI repo.
- Principle II (ADRs): ADR-070 governs this feature and is now reflected in research + data-model + contracts.
- Principle III (provider abstraction): preserved — picker still reads `extractor` role; embedder still reads `embedder` role.
- Principle IV (Pydantic): preserved — v2 types are Pydantic; bridging mappers (`candidate_mapper.py`) emit v2 Pydantic models.
- Principle V (config): no config changes required (verified in research.md).
- Principle VI (DB writes): writes go to v2 tables only — verified in data-model.md.
- Principle VII (Redis): writes go to v2 cache namespace only — verified in research.md.
- Principle VIII (API contract): byte-level field parity asserted in contracts/.
- Principle IX (testing): test rewrite plan captured in research.md.
- Principle X (git): no change to conventions.

**Final gate**: Pass. No `Complexity Tracking` entries needed.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| _(none)_ | — | — |
