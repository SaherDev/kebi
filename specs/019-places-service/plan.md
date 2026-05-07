# Implementation Plan: PlacesService — Shared Data Layer for Place Storage and Enrichment

**Branch**: `019-places-service` | **Date**: 2026-04-14 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/019-places-service/spec.md`

## Summary

> **Scope override (2026-04-14, after Phase 1 design):** The brief originally said "Do not modify ExtractionService, RecallService, ConsultService" and "Do not wire to any existing service." The user has explicitly overridden both: **one `PlaceObject` shape is shared everywhere in the app; no service uses a different shape or different attributes.** Feature 019 therefore (a) ships the new data layer AND (b) migrates every existing reader/writer of the old `Place` ORM fields (and every service-local "place" intermediate type) to `PlaceObject` / `PlaceCreate` in the same PR. The two-revision Alembic rollout proposed in research.md Decision 2 is **superseded** — see "Decision 2 (revised)" below. There is now one Alembic revision that adds new columns, runs the seed migration, and drops the legacy columns in the same migration. The full migration manifest is in [§ Code Migration Manifest](#code-migration-manifest) at the bottom of this file.

Build `PlacesService` as the shared data layer that all three agent tools (save, recall, consult) call, and migrate every existing service that reads or writes a "place" to use `PlaceObject` everywhere. It owns three storage tiers — Tier 1 PostgreSQL (permanent, our data only, zero provider-sourced columns beyond the namespaced identifier), Tier 2 Redis geo cache (30-day TTL), Tier 3 Redis enrichment cache (4-hour TTL) — and exposes a single unified `PlaceObject` Pydantic shape. Reads trust upstream callers for authorization (Q1). Cache backend errors degrade gracefully on read and are best-effort on write; permanent-store errors are fatal (Q2). `create()` is strict — collisions raise `DuplicatePlaceError` exposing the existing `place_id` (Q3). `create_batch()` is all-or-nothing (Q4). `enrich_batch()` dedupes by provider identifier internally, so duplicate input positions consume one cache slot and one fetch-cap slot (Q5). The cache-fetch path uses one MGET per tier and one parallel `asyncio.gather` for misses, capped per request via config.

This feature is built **standalone** — it does not wire into ExtractionService, RecallService, ConsultService, or any route. Wiring lands in subsequent features.

## Technical Context

**Language/Version**: Python 3.11 (constitution ADR-006: >=3.11,<3.13)
**Primary Dependencies**: SQLAlchemy 2.x async + asyncpg (Tier 1), redis.asyncio (Tier 2 + 3), Pydantic 2.10 (all I/O models), Alembic (schema migration), httpx (existing GooglePlacesClient — reused), Langfuse (tracing on provider calls only), pytest + pytest-asyncio (test runner)
**Storage**: PostgreSQL via SQLAlchemy async (system of record: `places` table); Redis (two key prefixes: `places:geo:{provider_id}` 30-day TTL, `places:enrichment:{provider_id}` 4-hour TTL)
**Testing**: pytest in `tests/core/places/` mirroring `src/kebi/core/places/`. Mock SQLAlchemy `AsyncSession`, mock `redis.asyncio.Redis`, mock `PlacesClient` Protocol. `asyncio_mode = "auto"` (already configured in repo).
**Target Platform**: Linux (Railway containerized FastAPI, Python 3.11)
**Project Type**: Single project — internal library module within an existing Python service. No new HTTP surface; the data layer is invoked only by other in-process services.
**Performance Goals**: Read & write paths: one cache round trip per tier per request (FR-018, FR-019, FR-022, SC-004). Provider fetch fan-out via `asyncio.gather`, capped at `config.places.max_enrichment_batch` (default 10).
**Constraints**: `mypy --strict` must pass on the new module (constitution IX). All boundaries Pydantic-typed (constitution IV). No raw dicts. No SQLAlchemy outside `core/places/repository.py`. Namespace string `"{provider}:{external_id}"` constructed only inside the repository; parsed only inside `PlacesService.enrich_batch` to strip the prefix before client calls — nowhere else.
**Scale/Scope**: Designed for tens-to-low-thousands of saved places per user, batches of up to a few dozen places per enrichment call. Per-request external-fetch cap defaults to 10.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Constitution clause | Status | Notes |
|---|---|---|
| **I. Repo Boundary** | ✅ | All work is AI/data layer inside this repo. No UI, auth, or NestJS dependency. NestJS still does not touch Redis or AI tables. |
| **II. ADRs are Constraints** | ⚠️ **VIOLATION** — see Complexity Tracking | Spec FR-005a (raise `DuplicatePlaceError` on collision) directly contradicts **ADR-041**, which mandates upsert semantics on `(external_provider, external_id)` collision. The spec also renames the columns from `(external_provider, external_id)` → `provider_id` (namespaced) and removes the composite uniqueness in favor of a single-column unique index. Both changes need a superseding ADR before any code lands. |
| **III. Provider Abstraction (NON-NEGOTIABLE)** | ✅ | The data layer references no model names. The `PlacesClient` Protocol already exists in `core/places/places_client.py` (ADR-049). Concrete `GooglePlacesClient` is reused as-is. No new logical role added to `models.yaml`. |
| **IV. Pydantic Everywhere** | ✅ | Every public method takes/returns Pydantic models or primitives. No raw dicts cross module boundaries. `HoursDict` is a `TypedDict` because it round-trips through Redis JSON; this stays inside the cache module and is wrapped in Pydantic on the way out. |
| **V. Configuration Rules** | ✅ | New `places:` section added to `config/app.yaml` (committed, non-secret). No new secrets. TTLs and fetch cap loaded via existing `get_config()`. |
| **VI. Database Write Ownership** | ✅ | `places` table is owned by this repo's Alembic. No NestJS column overlap. The schema change extends, renames, and drops columns within this repo's ownership; no product-side coordination needed beyond NestJS not currently joining on the dropped columns (verified — NestJS does not join on `cuisine`, `price_range`, `lat`, `lng`, `address`, `confidence`, `validated_at`, `external_provider`, `external_id`; per ADR-030 NestJS only writes `users` and `user_settings`). |
| **VII. Redis Ownership** | ✅ | New key prefixes `places:geo:` and `places:enrichment:` added to the existing Redis instance. No new Redis instance, no new connection pool. |
| **VIII. API Contract** | ✅ | No new HTTP route added in this feature. The PlacesService is purely internal. The (eventual) save/recall/consult tools that consume it ride on the existing `POST /v1/chat` route per ADR-052. Bruno collection is unchanged. |
| **IX. Testing** | ✅ | `tests/core/places/` mirrors `src/kebi/core/places/`. Every new module gets a test file. Mocks at the right boundaries (no real Redis or DB in unit tests). |
| **X. Git & Commits** | ✅ | Branch is `019-places-service` (spec-kit numbered, branched from `dev`). Commits will use `feat(places)` / `feat(db)` / `chore(config)` scopes per `.claude/rules/git.md`. |

**Gate verdict**: ⚠️ **FAIL on Constitution clause II**. Two unresolved ADR conflicts must be resolved in Phase 0 (research) before tasks can be generated. See Complexity Tracking for the full list and proposed resolutions.

There is also one **out-of-spec implementation conflict** that the brief itself contains, surfaced here for the user:

- The brief says "Do not modify ExtractionService" but also says drop `address`, `lat`, `lng`, `cuisine`, `price_range`, `confidence`, `validated_at`, `external_provider` from the `places` table. ExtractionService's `persistence.py:96-110` constructs a `Place(...)` ORM row populating exactly those columns and writes it through `SQLAlchemyPlaceRepository.save()`. Dropping the columns will crash extraction at runtime. Either ExtractionService is modified (contradicting the brief) or the migration is staged across multiple PRs (slow and risky). Phase 0 research proposes a resolution; the user must accept it before tasks land.

## Project Structure

### Documentation (this feature)

```text
specs/019-places-service/
├── plan.md              # This file (/speckit.plan output)
├── spec.md              # /speckit.specify output (already exists, clarified)
├── research.md          # Phase 0 output (this command)
├── data-model.md        # Phase 1 output (this command)
├── quickstart.md        # Phase 1 output (this command)
├── contracts/
│   └── places-service.md  # Phase 1 — internal API contract for PlacesService
└── checklists/
    └── requirements.md  # /speckit.specify checklist
```

### Source Code (repository root)

```text
src/kebi/core/places/
├── __init__.py                # NEW — re-exports PlacesService + all models
├── places_client.py           # EXISTING (ADR-049) — PlacesClient Protocol + GooglePlacesClient. Touched only to add get_place_details() per the brief.
├── models.py                  # NEW — PlaceObject, PlaceCreate, enums, attributes, GeoData, PlaceEnrichment, HoursDict, DuplicatePlaceError
├── repository.py              # NEW — PlacesRepository (renamed from brief's "PlaceRepository" to avoid collision with db/repositories/place_repository.py — see research.md decision)
├── cache.py                   # NEW — PlacesCache (single class, Tier 2 + Tier 3 methods)
└── service.py                 # NEW — PlacesService (orchestrates all three tiers)

src/kebi/db/
├── models.py                  # MODIFIED — Place ORM model schema reshaped (add place_type, subcategory, tags JSONB, attributes JSONB, source; rename external_provider+external_id → provider_id; drop legacy Google-sourced columns). See data-model.md.
└── repositories/
    └── place_repository.py    # UNTOUCHED in this feature — kept for ExtractionService until ExtractionService is migrated in a follow-up. (See Complexity Tracking item 4.)

alembic/versions/
└── XXXXXX_places_service_schema.py   # NEW — generated by `alembic revision --autogenerate`. Header comment instructs operator to run scripts/seed_migration.py BEFORE upgrade head.

scripts/
└── seed_migration.py          # NEW — relocates legacy cuisine/price_range into attributes JSONB; seeds Redis geo cache from rows that had lat/lng/address/provider_id; runs to completion before alembic upgrade head.

config/
└── app.yaml                   # MODIFIED — add `places:` section with cache_ttl_days, max_enrichment_batch.

tests/core/places/
├── __init__.py                # NEW
├── test_place_object.py       # NEW — Pydantic shape tests
├── test_repository.py         # NEW — repository behavior with mocked AsyncSession
├── test_cache.py              # NEW — PlacesCache behavior (both tiers) with mocked Redis
└── test_places_service.py     # NEW — service behavior with all dependencies mocked
```

**Structure Decision**: Single project (Option 1) — this is a new module inside the existing `src/kebi/core/` package, following ADR-002 hybrid directory layout. No new top-level project. No new HTTP surface. The new module sits next to the existing `places_client.py` (ADR-049), so the Tier-2/Tier-3 cache logic and the Protocol live in the same package.

## Phase 0: Outline & Research

See [research.md](./research.md). Phase 0 resolves the unresolved decisions blocking the Constitution Check:

1. **ADR-041 supersession** — write a new ADR (`ADR-054`) capturing the move from upsert to strict-create with explicit duplicate-detection lookup. The new ADR replaces both the upsert semantics and the composite-key field naming.
2. **`(external_provider, external_id)` → `provider_id`** — formalize the column rename and the unique-index change in the same `ADR-054`.
3. **Coexistence with ExtractionService during the transition** — propose a two-step rollout: (a) introduce the new schema and the new `PlacesRepository` in this feature; (b) update ExtractionService in a follow-up feature to use the new repository, with a temporary compatibility shim in the meantime. Document the precise shim shape so the user can approve or reject before tasks are generated.
4. **Naming collision** — class name decision: keep brief's `PlaceRepository` (require fully qualified imports forever) or rename the new one to `PlacesRepository` (matches `PlacesService`, no collision). Recommendation in research.md is `PlacesRepository`.
5. **Best-practice notes** — batch INSERT…RETURNING semantics in SQLAlchemy 2.x async; Redis `MGET`/pipelined `SET` patterns; Pydantic JSON serialization for `TypedDict` round-trip through Redis; `asyncio.gather` with bounded concurrency.

**Output**: research.md (next file written by this command).

## Phase 1: Design & Contracts

**Prerequisites**: research.md complete.

1. **Data model** → [data-model.md](./data-model.md): the exact column list for the new `places` table, every Pydantic shape, the unique constraints and indexes, the JSON serialization contract for `HoursDict`, and the relocation table for the seed migration.
2. **Contracts** → [contracts/places-service.md](./contracts/places-service.md): the internal API contract for `PlacesService`. Method-by-method signatures, behavior, error types, and freshness-indicator rules. This is the contract the save/recall/consult tools (built in later features) will code against.
3. **Quickstart** → [quickstart.md](./quickstart.md): how to verify the feature locally — install, configure, run migrations, run tests, and exercise the service via a small Python REPL recipe that mocks the provider client.
4. **Agent context** → run `.specify/scripts/bash/update-agent-context.sh claude` after the design files are in place so future Claude Code sessions have an updated CLAUDE.md fragment for this feature.

**Output**: data-model.md, contracts/places-service.md, quickstart.md, updated agent context file.

## Constitution Check — Post-Design Re-Evaluation

After Phase 0 (research.md) and Phase 1 (data-model.md, contracts/places-service.md, quickstart.md) are complete, re-evaluating each constitution clause:

| Clause | Status | Notes after design |
|---|---|---|
| I. Repo Boundary | ✅ | No change. |
| II. ADRs as Constraints | ⚠️ **Still requires user approval** before tasks. The two new ADRs (054, 055) drafted in research.md cleanly resolve the conflicts, but they need to be accepted into `docs/decisions.md` before `/speckit.tasks` so the constraint set is consistent. The data-model.md and contracts/places-service.md are written assuming both ADRs are accepted. |
| III. Provider Abstraction | ✅ | Confirmed — no model names anywhere; `PlacesClient` Protocol unchanged in shape (one new method `get_place_details` added, per the brief). |
| IV. Pydantic Everywhere | ✅ | Every public surface is Pydantic. `HoursDict` is a `TypedDict` because Pydantic 2 round-trips it cleanly through JSON, and it never crosses a function boundary as a raw dict — it is always inside a `PlaceEnrichment` or `PlaceObject`. |
| V. Configuration Rules | ✅ | New `places:` section in `config/app.yaml` (committed). New `PlacesConfig` Pydantic in `core/config.py`. No new secrets. |
| VI. Database Write Ownership | ✅ | Verified explicit list of legacy column drops; none are read or written by NestJS. |
| VII. Redis Ownership | ✅ | New `places:geo:*` and `places:enrichment:*` key prefixes; no overlap with existing prefixes. |
| VIII. API Contract | ✅ | No new HTTP route. ADR-052 chat consolidation is unaffected. |
| IX. Testing | ✅ | Five new test files mirror the new module. `mypy --strict` passes by construction (every model is typed). |
| X. Git & Commits | ✅ | Branch is `019-places-service`. Commit scopes will be `places`, `db`, `config`, `chore`. |

**Post-design verdict**: ⚠️ **Still gated on user acceptance of ADR-054 and ADR-055** (research.md Decisions 1 and 2). Once those are accepted, the gate is GREEN and `/speckit.tasks` can run.

## Complexity Tracking

> Filled because Constitution Check has unresolved violations.

| Violation / Open Decision | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| **(1) Supersede ADR-041 (upsert semantics)** with strict-create + `DuplicatePlaceError` | Spec FR-005a (clarified Q3) is explicit and the user picked it. ADR-041's upsert hides intent at the data layer and was made when only the extraction pipeline was a caller. | **Keep ADR-041 (upsert)**: rejected because it forces the save tool to silently lose information from the second call (e.g. a TikTok extraction overwriting a manual save) and removes the caller's ability to detect a collision and surface it. |
| **(2) Rename `(external_provider, external_id)` → `provider_id`** (single namespaced column with partial unique index) | Brief and spec require it. The namespaced form moves provider-namespace ownership inside the repository per FR-002. | **Keep composite columns**: rejected because every read path would have to remember to construct/parse the pair, and the duplicate-detection invariant (FR-005) is harder to enforce with a partial composite unique constraint that allows nulls in either side. Single-column partial unique is cleaner. |
| **(3) Drop legacy Google-sourced columns from `places`** (`address`, `lat`, `lng`, `cuisine`, `price_range`, `photo_url`, `hours`, `rating`, `phone`, `popularity`, `confidence`, `validated_at`, `ambiance`) | Brief: zero Google content in PostgreSQL beyond the namespaced ID. Tier 1 owns *our* data; Tier 2/3 own provider data. | **Keep them as nullable**: rejected because it leaves the duplication-of-truth between Postgres and Redis the brief explicitly forbids, and lets stale data drift indefinitely with no TTL. |
| **(4) Conflict between "Do not modify ExtractionService" and the schema drop — RESOLVED BY USER OVERRIDE** | ~~The brief asserts both, but ExtractionService writes directly to the columns that get dropped.~~ | **Resolved 2026-04-14**: user explicitly overrode the brief's "do not modify" line. Feature 019 now migrates ExtractionService AND every other reader/writer of legacy `Place` fields in the same PR. Single Alembic revision. See "Code Migration Manifest" section. |
| **(5) Class-name collision: `PlaceRepository`** | `db/repositories/place_repository.py` already exports a `PlaceRepository` Protocol used by ExtractionService. Brief asks for a new `PlaceRepository` in `core/places/repository.py`. | **Rename the new one to `PlacesRepository`** (plural, mirrors `PlacesService`). Rejected alternative: keep both classes named `PlaceRepository` in different modules — works but invites import bugs and confuses code search. |
| **(6) `cuisine` → `attributes.cuisine` relocation breadth** | Spec FR-014 puts cuisine inside structured attributes JSONB, removing the dedicated column. Existing rows have data in the column. | **Drop and re-extract**: rejected because it loses information that took prior LLM work to produce. Relocation script preserves it. |
| **(7) Existing `Place.address` is `nullable=False`** | The current schema has `address` as NOT NULL. Dropping it requires either making it nullable first (with a default) or running the relocation seed BEFORE the drop. | **Use the relocation step**: existing rows' `address`/`lat`/`lng` get seeded into Redis under the namespaced key (where present) before the column is dropped. Rows lacking a `provider_id` lose location data permanently, and that is acceptable: those rows had no way to be re-fetched anyway, so the location data is unreliable. The relocation script logs the count of dropped-and-not-relocated location values so the operator can see what was lost. |

The decisions in items (1)–(7) **must be approved by the user** before `/speckit.tasks` runs. Phase 0 research presents them as recommendations with rationale.

---

## Code Migration Manifest

This section enumerates **every file** that must change in feature 019 to satisfy the user override "one `PlaceObject` shape shared everywhere; no service uses different shape or different attributes." Generated from a static scan of `src/kebi/` and `tests/` for references to legacy `Place` fields and intermediate place types.

### Types eliminated (deleted from the codebase)

| Type | Current location | Replaced by | Reason |
|---|---|---|---|
| `ExtractionResult` | `src/kebi/core/extraction/types.py` | `PlaceCreate` (write) and `PlaceObject` (read) | One place shape app-wide |
| `SavedPlace` (API response) | `src/kebi/api/schemas/extract_place.py` | `PlaceObject` | One place shape app-wide |
| `CandidatePlace` | `src/kebi/core/extraction/types.py` (or wherever it currently lives) | `PlaceObject` | One place shape app-wide |
| `SQLAlchemyPlaceRepository` | `src/kebi/db/repositories/place_repository.py` | `PlacesRepository` (in `core/places/repository.py`) | Consolidated under PlacesService |
| `PlaceRepository` Protocol (legacy) | `src/kebi/db/repositories/place_repository.py` | None — `PlacesRepository` is the only repo type now | Consolidated |

### Types KEPT (with justification)

| Type | Where | Why kept |
|---|---|---|
| `_NERPlace` (private LLM output dataclass) | `core/extraction/enrichers/llm_ner.py`, `whisper_audio.py`, `subtitle_check.py` | This is the **LLM's structured output schema** for an NER prompt — it is the wire format Instructor uses to constrain the LLM's response. It is private to a single enricher file and never crosses a service boundary. The enricher converts `_NERPlace → PlaceCreate` immediately on the same line where it returns. The user's "one shape everywhere" rule applies to the shape that flows *between* services, not to the private LLM-output schema of one enricher. **Keep `_NERPlace`, ensure each enricher's exit point yields `PlaceCreate` only.** |
| `PlacesMatchResult`, `PlacesMatchQuality` | `core/places/places_client.py` | These are the **provider-call response shape** for `PlacesClient.validate_place()`, not a "place" the app passes around. They are mapped into `PlaceCreate` (with `external_id` + `provider`) by the caller. Untouched. |
| `Place` (SQLAlchemy ORM) | `src/kebi/db/models.py` | Stays — but its fields change. Legacy columns are dropped; new columns are added. The class itself remains; only `PlacesRepository` reads/writes it. Outside the repository, code only sees `PlaceObject`. |

### Source files to MODIFY

| # | File | Change |
|---|---|---|
| 1 | `src/kebi/db/models.py` | Reshape `Place` ORM: drop `address`, `cuisine`, `price_range`, `lat`, `lng`, `external_provider`, `external_id`, `confidence`, `validated_at`, `ambiance`. Add `place_type`, `subcategory`, `tags JSONB`, `attributes JSONB`, `provider_id`. Drop `uq_places_provider_external` composite constraint. Add partial unique index on `provider_id`. Add `(user_id, place_type)` composite index. Add FTS GIN index on `place_name + subcategory`. |
| 2 | `src/kebi/core/extraction/persistence.py` | Replace direct `Place(...)` constructor (lines 96-110) and `_place_repo.save()` call (line 111) with `places_service.create_batch([PlaceCreate(...)])`. Convert `ExtractionResult` (or whatever the persistence input is) into `PlaceCreate`, mapping `address/lat/lng/cuisine/price_range/external_*` into `attributes` JSONB and `provider`+`external_id`. Catch `DuplicatePlaceError` to produce `PlaceSaveOutcome(status="duplicate")`. |
| 3 | `src/kebi/core/extraction/types.py` | Delete `ExtractionResult` dataclass. Replace its usage with `PlaceCreate` (write side) or `PlaceObject` (read side). Delete `CandidatePlace` if it lives here, or migrate it to a thin alias for `PlaceObject` with the candidate-specific fields folded into `attributes`. |
| 4 | `src/kebi/core/extraction/service.py` | Replace `ExtractionResult` references with `PlaceCreate`/`PlaceObject` as appropriate. Replace any code that reads `result.address`/`result.cuisine`/`result.external_*` with `place.attributes.*` / `place.provider_id`. The service's response is `list[PlaceObject]`, not `list[SavedPlace]`. |
| 5 | `src/kebi/core/extraction/validator.py` | Stops constructing `ExtractionResult`. Constructs `PlaceCreate` instead, mapping the extracted fields into `attributes` JSONB and `provider`/`external_id`. |
| 6 | `src/kebi/core/extraction/handlers/extraction_pending.py` | Reads from `PlaceObject` instead of `ExtractionResult`. The status-payload builder maps from `PlaceObject.attributes` and `PlaceObject.provider_id`. |
| 7 | `src/kebi/core/extraction/enrichers/llm_ner.py` | Keep `_NERPlace` as the LLM output schema. Change the function that returns the enriched candidates so it yields `PlaceCreate` (mapping `_NERPlace.cuisine → PlaceAttributes.cuisine`, `_NERPlace.price_range → PlaceAttributes.price_hint` with the `low→cheap` etc. mapping). |
| 8 | `src/kebi/core/extraction/enrichers/whisper_audio.py` | Same: keep the local `_NERPlace`, output `PlaceCreate`. |
| 9 | `src/kebi/core/extraction/enrichers/subtitle_check.py` | Same. |
| 10 | `src/kebi/core/extraction/extraction_pipeline.py` | The pipeline's intermediate state passes `PlaceCreate`/`PlaceObject` between nodes. No more `ExtractionResult`. |
| 11 | `src/kebi/core/extraction/dedup.py` | Dedup operates on `PlaceCreate.provider` + `PlaceCreate.external_id` (or on `PlaceObject.provider_id` for already-saved places). |
| 12 | `src/kebi/db/repositories/recall_repository.py` | The hybrid SQL (lines 188-258) currently selects `p.address, p.cuisine, p.price_range, p.lat, p.lng, p.external_id`. Rewrite to select `p.place_type, p.subcategory, p.tags, p.attributes, p.provider_id`. Map the SQL row into `PlaceObject` (Tier 1 only — the recall service then calls `places_service.enrich_batch(geo_only=True)` to attach Tier 2). FTS index in the SQL must point to the new `places_fts_idx`. |
| 13 | `src/kebi/core/recall/service.py` (if it exists separately) | Returns `list[PlaceObject]`. Calls `places_service.enrich_batch(places, geo_only=True)` after the recall query. |
| 14 | `src/kebi/core/taste/service.py` | Lines 191-194: replace `place.price_range` / `place.ambiance` with `place.attributes.price_hint` / `place.attributes.ambiance` where `place: PlaceObject`. The taste service receives `PlaceObject` instances (not ORM rows) from upstream. |
| 15 | `src/kebi/core/consult/service.py` | Wherever consult passes "places" between nodes, the type is `PlaceObject`. Consult invokes `places_service.enrich_batch(geo_only=False)` for full enrichment. Any code reading `place.cuisine` / `place.address` becomes `place.attributes.cuisine` / `place.address` (Tier 2 field). |
| 16 | `src/kebi/core/consult/types.py` | Delete any local "place" dataclass. Use `PlaceObject` directly. |
| 17 | `src/kebi/core/ranking/service.py` | Same — operates on `PlaceObject`. Reads `place.attributes.*` for ranking signals; reads `place.lat/lng` from Tier 2 fields if present (otherwise rank without distance). |
| 18 | `src/kebi/core/intent/intent_parser.py` | If it returns or accepts a place type, use `PlaceObject` / `PlaceCreate`. (Likely just an enum or a search query — minimal change.) |
| 19 | `src/kebi/core/events/handlers.py` | Any `PlaceSaved` event payload referencing legacy fields uses `PlaceObject` instead. |
| 20 | `src/kebi/api/schemas/extract_place.py` | Delete `SavedPlace`. The route returns `list[PlaceObject]` directly. Update any imports across the api package. |
| 21 | `src/kebi/api/deps.py` | Replace `Depends(get_place_repository)` (which returns `SQLAlchemyPlaceRepository`) with `Depends(get_places_service)` returning a fully-wired `PlacesService(repo, cache, client)`. Add the new factory functions. |
| 22 | `src/kebi/db/repositories/place_repository.py` | **DELETE FILE** (after callers migrated). |
| 23 | `src/kebi/db/repositories/__init__.py` | Remove `PlaceRepository` and `SQLAlchemyPlaceRepository` from the re-export list. |
| 24 | `src/kebi/db/__init__.py` | Remove any re-export of legacy place types. |
| 25 | `config/app.yaml` | Add `places:` section (`cache_ttl_days`, `max_enrichment_batch`). |
| 26 | `src/kebi/core/config.py` | Add `PlacesConfig` Pydantic submodel; expose via `AppConfig.places`. |

### Source files to CREATE

| # | File | Purpose |
|---|---|---|
| 27 | `src/kebi/core/places/models.py` | All Pydantic models from data-model.md §1. |
| 28 | `src/kebi/core/places/repository.py` | `PlacesRepository` class (note: plural, see research.md Decision 3). |
| 29 | `src/kebi/core/places/cache.py` | `PlacesCache` — single class holding both Tier 2 (`get_geo_batch`/`set_geo_batch`) and Tier 3 (`get_enrichment_batch`/`set_enrichment_batch`) methods. Shares one Redis client reference, one TTL (`config.places.cache_ttl_days * 86400`), two key prefixes (`places:geo:`, `places:enrichment:`). |
| 31 | `src/kebi/core/places/service.py` | `PlacesService`. |
| 32 | `src/kebi/core/places/__init__.py` | Re-exports per contracts/places-service.md. (Edit existing file — keep `PlacesClient`, `GooglePlacesClient`, `PlacesMatchResult`, `PlacesMatchQuality` exports too.) |
| 33 | `alembic/versions/XXX_places_service_schema.py` | Single revision: add new columns, backfill `provider_id` and `attributes` JSONB, add new indexes, drop legacy columns and the legacy composite unique constraint. Header comment instructs operator to run `scripts/seed_migration.py` before `alembic upgrade head`. |
| 34 | `scripts/seed_migration.py` | Per data-model.md §4. |

### Existing client method to ADD

| # | File | Change |
|---|---|---|
| 35 | `src/kebi/core/places/places_client.py` | Add `get_place_details(external_id: str) -> dict | None` method to the `PlacesClient` Protocol AND to `GooglePlacesClient`. The Google implementation calls Google Places **Place Details** API with field mask `geometry,formatted_address,opening_hours,rating,formatted_phone_number,photos,user_ratings_total` and maps the response into `{"lat", "lng", "address", "hours", "rating", "phone", "photo_url", "popularity"}`. (Per ADR-049, this method belongs on `PlacesClient`, alongside `validate_place`/`discover`/`validate`/`geocode`.) |

### Tests to MODIFY

| # | File | Change |
|---|---|---|
| 36 | `tests/core/extraction/test_types.py` | Replace `ExtractionResult`/`CandidatePlace` test fixtures with `PlaceCreate`/`PlaceObject`. |
| 37 | `tests/core/extraction/test_persistence.py` | Replace `_make_result()` factory with a `PlaceCreate` factory. Replace dedup-based assertions with `DuplicatePlaceError` assertions. Mock `PlacesService.create_batch` instead of `SQLAlchemyPlaceRepository.save`. |
| 38 | `tests/core/extraction/test_validator.py` | Replace `ExtractionResult` assertions with `PlaceCreate` assertions. Field accesses `.address`/`.cuisine`/`.lat`/`.lng` become `.attributes.*` or `.external_id`/`.provider`. |
| 39 | `tests/core/extraction/handlers/test_extraction_pending_handler.py` | Replace `ExtractionResult` construction with `PlaceObject` construction. |
| 40 | `tests/core/extraction/enrichers/test_llm_ner.py` | Keep `_NERPlace` test (it is the LLM output schema). Add an additional test that asserts the enricher's exit converts `_NERPlace → PlaceCreate` correctly. |
| 41 | `tests/core/chat/test_service.py` | Replace `SavedPlace` construction with `PlaceObject` construction. |
| 42 | `tests/core/extraction/test_dedup.py` | Replace `CandidatePlace.cuisine` with `PlaceCreate.attributes.cuisine` (or `PlaceObject.attributes.cuisine`, depending on which side of the pipeline the dedup runs on). |

### Tests to CREATE

Per the spec's testing section, the new module gets: `tests/core/places/__init__.py`, `test_place_object.py`, `test_repository.py`, `test_cache.py` (covers both Tier 2 and Tier 3 methods on `PlacesCache`), `test_places_service.py`. Already covered in the project structure section above.

### Bruno collection

| # | File | Change |
|---|---|---|
| 43 | `kebi-config/bruno/*.bru` (any file referencing the extract-place response) | Update example response payloads to match `PlaceObject` shape (no `address`/`cuisine`/`external_provider`/`external_id` fields; instead `place_type`/`subcategory`/`tags`/`attributes.cuisine`/`provider_id`). |

### ⚠️ Wiring blast radius — `get_batch` silent drop

`PlacesService.get_batch(place_ids)` and `PlacesRepository.get_batch(place_ids)` do **not** return `None` placeholders for missing rows. They omit missing IDs entirely, so the output list may be shorter than the input list.

**Any caller that needs positional alignment between input and output MUST use `get(place_id)` per ID and handle `None` explicitly.**

Audit targets in this feature (every site that takes a list of place IDs and joins it positionally against the result):

| File | Risk | Required fix during migration |
|---|---|---|
| `src/kebi/core/ranking/service.py` | If ranking pre-computes parallel arrays of scores indexed by input position, a missing place will misalign the score array against the place array | Migration task T052 must call `get()` per ID for any positional join, OR explicitly re-key scores by `place_id` after the batch fetch and tolerate missing entries |
| `src/kebi/core/consult/service.py` | Consult LangGraph nodes pass lists of candidate place IDs; if any node does `zip(input_ids, get_batch(...))` it will silently misalign | Migration task T050 must scan for `zip(...)` over `get_batch` output and replace with per-ID `get()` or a dict-keyed merge |
| `src/kebi/db/repositories/recall_repository.py` rewrite | The hybrid SQL already returns its own row set with all needed columns; it does NOT call `get_batch`. Safe. | None |
| `src/kebi/core/extraction/persistence.py` | After `create_batch` succeeds it returns `len(items)` `PlaceObject`s in order. `get_batch` is not called here. Safe. | None |
| Any future caller introduced in this feature | — | Code-review check: `grep -n 'get_batch' src/kebi/` and verify no positional-alignment assumptions |

The contract for this is already explicit in `contracts/places-service.md` § `get_batch`.

### What this manifest does NOT cover

- Wiring `PlacesService.enrich_batch` into a route handler (no new HTTP route in this feature; existing routes that return `PlaceObject`s already get them from the migrated services).
- Multi-provider routing (Foursquare fallback). Deferred.
- Backfill of `place_type` for legacy rows that have no extractable type. The seed migration sets `place_type = NULL` for rows without enough information; the column is added as NULLABLE in this revision and tightened to NOT NULL in a follow-up only after every legacy row has been re-classified by re-running extraction.
- Any change to the embeddings table or the embedding pipeline. Embeddings still key on `place_id` (the internal UUID), which is unchanged.

### Effort / risk note

This is **substantially larger** than the original brief's scope (the brief said "Do not modify ExtractionService, RecallService, or ConsultService"). The user override absorbs all those services into feature 019. Approximate file count:

- **Modified**: 26 source files + 7 test files + Bruno files ≈ 35 files
- **Created**: 8 new source files + 5 new test files = 13 files
- **Deleted**: 1 source file (`db/repositories/place_repository.py`)
- **Schema**: 1 Alembic revision (single, not split)

The `/speckit.tasks` output should reflect this expanded scope. The user should expect the task list to be ~50-60 tasks, not the 10-15 the original brief implied.
