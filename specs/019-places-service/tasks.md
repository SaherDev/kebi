# Tasks: PlacesService — Shared Data Layer for Place Storage and Enrichment

**Feature**: 019-places-service
**Branch**: `019-places-service`
**Input**: Design documents from `/specs/019-places-service/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/places-service.md, quickstart.md

**Tests**: INCLUDED — the spec explicitly listed test files in Step 10 of the original brief, and the constitution (clause IX) requires every new module to ship with a test file.

**Organization**: Tasks are grouped by user story so each story can be implemented and tested independently. Phases 1–2 are blocking prerequisites; Phases 3–6 deliver the four user stories in priority order; Phase 7 is polish.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: User story label (US1–US4) for story-phase tasks only
- Exact file paths included in every description

---

## Phase 1: Setup (shared infrastructure)

**Purpose**: prerequisites that must exist before any code in the new module can compile or any service migration can land. No story label.

- [X] T001 Verify the working branch is `019-places-service` (run `git branch --show-current`); if not, abort and re-run `/speckit.specify`.
- [X] T002 Add ADR-054 to `docs/decisions.md` per the draft text in `specs/019-places-service/research.md` § Decision 1 — "PlacesService strict-create with explicit duplicate-detection lookup, supersedes ADR-041". Place above ADR-053. Mark ADR-041 as `superseded by ADR-054`.
- [X] T003 [P] Add the `places:` configuration section to `config/app.yaml` with keys `cache_ttl_days: 30`, `max_enrichment_batch: 10`. Place after the `memory:` section. (One TTL serves both the Tier 2 geo cache and the Tier 3 enrichment cache — `PlacesCache` uses `config.places.cache_ttl_days * 86400` seconds for both.)
- [X] T004 [P] Add `PlacesConfig` Pydantic submodel to `src/kebi/core/config.py` with fields `cache_ttl_days: int = 30`, `max_enrichment_batch: int = 10`. Wire it into `AppConfig` as `places: PlacesConfig = Field(default_factory=PlacesConfig)` so existing `app.yaml` files without the section still load.

---

## Phase 2: Foundational (blocks every user story)

**Purpose**: the Pydantic models, the ORM reshape, the migration, and the provider client extension. Every user story phase below needs all of these to be done first. No story label.

**⚠️ CRITICAL**: nothing in Phase 3+ can start until Phase 2 is complete.

- [X] T005 [P] Create `src/kebi/core/places/models.py` with all Pydantic models per `data-model.md` § 1: `PlaceType`, `PlaceSource`, `PlaceProvider` enums; `LocationContext`, `PlaceAttributes` (with `extra="forbid"`), `HoursDict` TypedDict, `GeoData`, `PlaceEnrichment`, `PlaceObject`, `PlaceCreate` (with `model_validator` that enforces "exactly zero or both of `external_id` and `provider`" and validates subcategory against the per-place_type vocabulary). Define `DuplicateProviderId` dataclass and `DuplicatePlaceError(Exception)` at the bottom of the file.
- [X] T006 [P] Create `tests/core/places/__init__.py` (empty file) so pytest discovers the new test directory.
- [X] T007 [P] Create `tests/core/places/test_place_object.py` with shape-only tests: `PlaceObject` constructs with Tier 1 fields only and `geo_fresh=False`, `enriched=False`; `PlaceAttributes` defaults all fields to `None`/`[]`; `HoursDict` round-trips through `model_dump_json` / `model_validate_json` preserving `timezone` key; `PlaceCreate` with `provider=PlaceProvider.google` + `external_id` constructs; `PlaceCreate` with only one of the two raises `ValueError`; `DuplicatePlaceError(conflicts=[...])` carries the list. Depends on T005.
- [X] T008 Reshape `src/kebi/db/models.py` `Place` class per `data-model.md` § 2.5: keep `id`, `user_id`, `created_at`, `updated_at`, `place_name`, `source_url`; add `place_type` (`String`, NOT NULL), `subcategory` (nullable), `tags` (`JSONB`, nullable), `attributes` (`JSONB`, nullable), `provider_id` (`String`, nullable), `source` (already exists, keep). Drop the legacy field declarations: `address`, `cuisine`, `price_range`, `lat`, `lng`, `external_provider`, `external_id`, `confidence`, `validated_at`, `ambiance`. Drop the `uq_places_provider_external` `UniqueConstraint` from `__table_args__`. (mypy will fail across the codebase after this — that is expected; Phase 3+ fixes the call sites.)
- [X] T009 Generate the Alembic migration file via `poetry run alembic revision --autogenerate -m "places_service_schema"`. Then hand-edit the generated file in `alembic/versions/` to: (a) add a header comment "RUN `python scripts/seed_migration.py` BEFORE `alembic upgrade head`"; (b) ensure the `provider_id` backfill UPDATE statement runs BEFORE the partial unique index is created; (c) drop `uq_places_provider_external` AFTER `uq_places_provider_id` is created and validated; (d) add the `(user_id, place_type)` composite index and the `places_fts_idx` GIN FTS index per `data-model.md` § 2.4. Depends on T008.
- [X] T010 Create `scripts/seed_migration.py` per `data-model.md` § 4. Reads each row from the legacy `places` table; relocates `cuisine` → `attributes.cuisine`, `price_range` → `attributes.price_hint` (with `low/mid/high → cheap/moderate/expensive` mapping; logs `unmapped_price_range`), `ambiance` → `attributes.ambiance`; seeds Redis `places:geo:{provider_id}` for rows with `lat/lng/address/provider_id` all present (single pipeline); logs `geo_data_lost_no_provider_id` for rows without `provider_id`; backfills `place_type` via the heuristic ladder (cuisine present → `food_and_drink`; nature/museum keyword in `place_name` → `things_to_do`; else `services` with `place_type_defaulted` log line). **Does NOT touch `subcategory`** — leaves it `NULL` for every legacy row (the LLM enricher will set it on the next extraction; a blanket cuisine→`restaurant` mapping is too broad). Idempotent: re-running does not corrupt data. Prints a counts report to stdout and `scripts/seed_migration.log`. **Operator review gate**: when any row was defaulted to `place_type='services'`, exit code is `2` (non-zero) and the report ends with a "REVIEW REQUIRED" warning. Re-running with `--accept-defaults` writes an `accepted_defaults` line to the log and exits `0`. The Alembic migration file's header comment instructs the operator to review the log and clear the gate before `alembic upgrade head`. Depends on T005, T008.
- [X] T011 [P] Add `get_place_details(external_id: str) -> dict | None` to the `PlacesClient` Protocol AND the `GooglePlacesClient` class in `src/kebi/core/places/places_client.py`. The Google implementation calls Google Places **Place Details** API with the field mask `geometry,formatted_address,opening_hours,rating,formatted_phone_number,photos,user_ratings_total` and maps the response to `{"lat", "lng", "address", "hours", "rating", "phone", "photo_url", "popularity"}`. The `hours` dict includes a `timezone` IANA key. Returns `None` on any HTTP failure (does NOT raise — the data layer treats failures per place as "no enrichment", per FR-026 / clarification Q2). Depends on T005 (uses no models from it directly, but the dict shape is referenced by `data-model.md` § 5).

---

## Session 2 Addendum (READ BEFORE STARTING PHASE 3)

Three additional changes must be applied during session 2. Do these BEFORE (or alongside) T012–T037. Two of them add new Phase-2-level work retroactively; the third rewrites a Phase-4 task.

### T011a — Config-driven `_build_description` for the embedding pipeline

The legacy `ExtractionService` builds its embedding input by concatenating `Place` ORM columns (`place_name`, `address`, `cuisine`, `price_range`, etc.). Those columns no longer exist. The replacement must read from `PlaceObject` Tier 1 fields only, driven by config so retrieval evals can re-tune without code changes.

Work:

1. Add `description_fields` and `description_separator` to `EmbeddingsConfig` in `src/kebi/core/config.py`:
   ```python
   class EmbeddingsConfig(BaseModel):
       dimensions: int = 1024
       description_separator: str = " | "
       description_fields: list[str] = [
           "place_name", "subcategory", "place_type",
           "cuisine", "ambiance", "price_hint",
           "tags", "good_for", "dietary",
           "neighborhood", "city", "country",
       ]
   ```
2. Add the matching `embeddings:` section to `config/app.yaml` alongside the existing `dimensions` key.
3. Replace the legacy `_build_description` (currently in `core/extraction/persistence.py`) with a `PlaceObject`-backed version:
   ```python
   def _build_description(self, place: PlaceObject) -> str:
       cfg = get_config().embeddings
       extractors: dict[str, Callable[[PlaceObject], str | None]] = {
           "place_name":   lambda p: p.place_name,
           "subcategory":  lambda p: p.subcategory,
           "place_type":   lambda p: p.place_type.value.replace("_", " "),
           "cuisine":      lambda p: p.attributes.cuisine,
           "ambiance":     lambda p: p.attributes.ambiance,
           "price_hint":   lambda p: p.attributes.price_hint,
           "tags":         lambda p: " ".join(p.tags) if p.tags else None,
           "good_for":     lambda p: " ".join(p.attributes.good_for) if p.attributes.good_for else None,
           "dietary":      lambda p: " ".join(p.attributes.dietary) if p.attributes.dietary else None,
           "neighborhood": lambda p: p.attributes.location_context.neighborhood if p.attributes.location_context else None,
           "city":         lambda p: p.attributes.location_context.city if p.attributes.location_context else None,
           "country":      lambda p: p.attributes.location_context.country if p.attributes.location_context else None,
       }
       parts = []
       for field in cfg.description_fields:
           extractor = extractors.get(field)
           if extractor:
               value = extractor(place)
               if value:
                   parts.append(value)
       return cfg.description_separator.join(parts)
   ```

**Constraints**:
- NO `address` in the description.
- NO Tier 2 (`lat`, `lng`, `address`) or Tier 3 (`hours`, `rating`, `phone`, `photo_url`, `popularity`) fields anywhere in this function. Embeddings describe *the place itself* — the cacheable live details and geo must never leak into the vector.
- The function takes `PlaceObject` only, not `PlaceCreate` — embedding happens after persistence, so the place already has a stable `place_id`.
- Unknown field names in `description_fields` are silently skipped (`extractors.get(field)` returns `None`). This is intentional so adding a future field to config doesn't immediately crash production.

**Note**: the `config/app.yaml` `embeddings:` section and the `EmbeddingsConfig` Pydantic extension have already been applied in this repo (see git status). The remaining work for T011a is replacing the `_build_description` method body — that happens as part of T019 (`core/extraction/persistence.py` migration). The addendum is recorded here so the next session doesn't miss it while rewriting persistence.

### T011b — `search_vector` generated tsvector column + new Alembic migration

Replace the inline `to_tsvector` expression in the existing FTS index with a `GENERATED ALWAYS AS ... STORED` column, so hybrid-search queries can filter and rank on `p.search_vector` directly without recomputing the vector per row. The generated column's expression must cover every field the retrieval evals care about — which is the same list as `embeddings.description_fields` on the text side.

Work:

1. **New Alembic revision** (separate from `9a1c7b54e2f0_places_service_schema.py`): name it something like `a1b2c3d4e5f6_places_search_vector_generated_column.py`. It runs AFTER the main schema migration.
2. **Upgrade body**:
   ```python
   op.execute("DROP INDEX IF EXISTS places_fts_idx")
   op.execute("""
       ALTER TABLE places ADD COLUMN search_vector tsvector
       GENERATED ALWAYS AS (
           to_tsvector('english',
               coalesce(place_name, '') || ' ' ||
               coalesce(subcategory, '') || ' ' ||
               coalesce(attributes->>'cuisine', '') || ' ' ||
               coalesce(attributes->>'ambiance', '') || ' ' ||
               coalesce(attributes->>'price_hint', '') || ' ' ||
               coalesce(attributes->'location_context'->>'neighborhood', '') || ' ' ||
               coalesce(attributes->'location_context'->>'city', '') || ' ' ||
               coalesce(attributes->'location_context'->>'country', '')
           )
       ) STORED
   """)
   op.execute("CREATE INDEX places_fts_idx ON places USING gin(search_vector)")
   ```
3. **Header comment** in the migration file:
   > Fields in this generated column must match `config/app.yaml` `embeddings.description_fields`. Changing `description_fields` requires a NEW migration to update this expression AND a full re-embedding of all saved places. The two lists are coupled by convention; there is no automated check. If you add a Tier 1 field to one, add it to the other.
4. **ORM sync**: add a read-only mapped column to `Place` in `src/kebi/db/models.py`:
   ```python
   search_vector: Mapped[str | None] = mapped_column(
       nullable=True, init=False, repr=False
   )
   ```
   Also remove the inline FTS `Index(..., postgresql_using="gin")` declaration from `__table_args__` in the same file — the new migration creates the index directly.
5. **Repository change**: `PlacesRepository` must exclude `search_vector` from `INSERT` and `UPDATE` statements (it is a generated column; PostgreSQL computes it automatically).

### T042 (rewritten) — Full `RecallRepository` rewrite for session 3

When session 3 gets to Phase 4, T042 is no longer "tweak the SELECT list". The current `RecallRepository` is also **missing the entire filter system** — it only implements the hybrid-search path. The rewrite delivers two modes in one repository: a pure-filter mode (no query → `SELECT ... WHERE ... ORDER BY created_at DESC`) and a hybrid mode (query present → vector + FTS + RRF with the same filter clauses applied).

#### 1. Input model — add to `src/kebi/core/recall/types.py`

```python
from dataclasses import dataclass, field
from datetime import datetime
from kebi.core.places.models import PlaceObject


@dataclass
class RecallFilters:
    place_type:       str | None = None
    subcategory:      str | None = None
    source:           str | None = None
    tags_include:     list[str] | None = None
    cuisine:          str | None = None
    price_hint:       str | None = None
    ambiance:         str | None = None
    neighborhood:     str | None = None
    city:             str | None = None
    country:          str | None = None
    max_distance_km:  float | None = None
    created_after:    datetime | None = None
    created_before:   datetime | None = None


@dataclass
class RecallResult:
    place:           PlaceObject
    match_reason:    str
    relevance_score: float | None = None
```

Delete the old `RecallRow` TypedDict — it carries legacy fields (`address`, `cuisine`, `price_range`, `lat`, `lng`, `external_id`) that no longer exist.

#### 2. Repository interface

```python
async def search(
    self,
    user_id: str,
    query: str | None,
    filters: RecallFilters,
    sort_by: Literal["relevance", "created_at"],
    limit: int,
    location: tuple[float, float] | None = None,
) -> tuple[list[RecallResult], int]:
    """Returns (results, total_count). total_count is the unfiltered-by-LIMIT count."""
```

Replace `hybrid_search(...)` with this single entry point. `count_saved_places` stays as-is.

#### 3. Mode selection

- **`query is None` → filter mode**: pure `SELECT` with `WHERE` clauses, `ORDER BY p.created_at DESC`, `LIMIT :limit`. No embedding, no FTS, no RRF.
- **`query is not None` → hybrid mode**: vector search + FTS on `p.search_vector` + RRF merge, then apply the same `WHERE` clauses as a post-RRF filter (or inline in the vector/FTS CTEs, whichever produces cleaner SQL). `sort_by="relevance"` uses RRF order; `sort_by="created_at"` re-sorts by `p.created_at DESC` after RRF.

#### 4. Filter → `WHERE` clause mapping (applies to both modes)

Build the `WHERE` clause list dynamically — only include a clause when the corresponding `RecallFilters` field is set. Parameters bound by name; no string interpolation of user data.

```python
# Tier 1 columns
if filters.place_type    is not None: "p.place_type = :place_type"
if filters.subcategory   is not None: "p.subcategory = :subcategory"
if filters.source        is not None: "p.source = :source"
if filters.created_after is not None: "p.created_at >= :created_after"
if filters.created_before is not None: "p.created_at <= :created_before"

# JSONB attribute paths
if filters.cuisine      is not None: "p.attributes->>'cuisine' = :cuisine"
if filters.price_hint   is not None: "p.attributes->>'price_hint' = :price_hint"
if filters.ambiance     is not None: "p.attributes->>'ambiance' = :ambiance"
if filters.neighborhood is not None: "p.attributes->'location_context'->>'neighborhood' = :neighborhood"
if filters.city         is not None: "p.attributes->'location_context'->>'city' = :city"
if filters.country      is not None: "p.attributes->'location_context'->>'country' = :country"

# JSONB array containment
if filters.tags_include is not None: "p.tags @> :tags_include::jsonb"
```

Every query also has the implicit `WHERE p.user_id = :user_id` clause.

#### 5. Distance filter is NOT a SQL clause

`max_distance_km` requires `lat`/`lng` from Redis (Tier 2). Do NOT add a geo join, a `ST_Distance`, or a PostGIS call to the SQL. Apply distance filtering **after** the repository returns, in the recall service:

1. Repository runs SQL and returns `(list[RecallResult], total_count)`.
2. Recall service calls `places_service.enrich_batch(places, geo_only=True)` to attach `lat`/`lng`/`address` from Redis.
3. Recall service filters out places where `geo_fresh is False` OR the computed distance from `location` exceeds `max_distance_km`. Use haversine in Python — no SQL needed.
4. Recall service recomputes `total_count` **only if distance filtering removed results**, otherwise returns the DB-level count unchanged. (Note: post-filter total is best-effort — it reflects the window actually delivered, not the full unfiltered set. Document this in the service docstring.)

#### 6. `match_reason` values

- Hybrid mode, both vector AND FTS matched the place: `"semantic + keyword"`
- Hybrid mode, vector only: `"semantic"`
- Hybrid mode, FTS only: `"keyword"`
- Filter mode (no query): `"filter"`

Drop every legacy match_reason string (`"Matched by name, cuisine, and semantic similarity"`, etc.).

#### 7. SELECT column list

Return Tier 1 only for both modes:

```sql
SELECT
    p.id,
    p.place_name,
    p.place_type,
    p.subcategory,
    p.tags,
    p.attributes,
    p.source_url,
    p.source,
    p.provider_id,
    p.created_at
FROM places p
WHERE p.user_id = :user_id
  AND <filter clauses>
ORDER BY <mode-specific>
LIMIT :limit
```

Hybrid mode's FTS expression uses `p.search_vector` directly (the generated column from T011b). Never `to_tsvector(...)` inline. Drop every legacy column from the SELECT — no `p.address`, `p.cuisine`, `p.price_range`, `p.lat`, `p.lng`, `p.external_id`.

Map each result row into a `PlaceObject` via a `_row_to_place_object` helper. `geo_fresh=False`, `enriched=False`, Tier 2/3 fields `None`.

#### 8. `total_count`

Run a separate `SELECT COUNT(*)` with the same `WHERE` clauses but no `LIMIT`, no `ORDER BY`, no RRF. Return as the second element of the tuple from `search()`. One extra round trip per call — acceptable for the recall use case.

For hybrid mode the `total_count` is the count of places matching the filter clauses, NOT the count of RRF-ranked candidates (those are pre-filtered by the RRF `min_rrf_score` threshold and inherently bounded by `candidate_multiplier * limit`). The count reflects "how many saved places *could* match this query if we paginated", which is what the frontend wants for "Showing 20 of 147".

#### 9. After the repository returns

The recall service (`core/recall/service.py`) does this in sequence:

1. `results, total_count = await repo.search(...)`
2. `places = [r.place for r in results]`
3. `enriched_places = await places_service.enrich_batch(places, geo_only=True)`
4. If `filters.max_distance_km` is set and `location` is non-None:
   - Filter `enriched_places` in Python: drop any with `geo_fresh is False` or haversine distance > `max_distance_km`.
   - Re-assemble `RecallResult`s from the filtered places, preserving `match_reason` and `relevance_score` from the original `results` (use a dict keyed by `place_id` to avoid index drift after filtering).
5. Return `{"results": [...], "total_count": total_count}` (or a Pydantic response model — whichever the route expects).

The repository itself never touches Redis, never computes distance, and never knows about `location`. The `location: tuple[float, float] | None = None` parameter on `search()` is accepted but **ignored** in the repository body — it exists only so the interface is self-documenting. The recall service is responsible for all geo work.

#### 10. Task wording for T042 / T043

- **T042** covers the repository rewrite: new types, new `search()` signature, two-mode SQL, filter clauses, `COUNT(*)` round-trip. No Redis, no distance, no `enrich_batch`.
- **T043** covers the recall service rewrite: two-step flow (`repo.search` → `enrich_batch` → optional distance filter → response assembly). Imports `RecallResult` + `RecallFilters` from `core/recall/types.py`. Wires `PlacesService` via the existing DI.

This replaces the current T042 wording ("rewrite the SELECT to read the new columns and materialize `PlaceObject`") and the previous addendum wording ("delete RecallRow, introduce RecallResult, rewrite hybrid SQL") with this complete two-mode specification.

---

## Phase 3: User Story 1 — Save a Place with Provider Identity (P1)

**Story goal**: a downstream caller can hand `PlacesService.create()` (or `create_batch()`) a freshly extracted place, get back a `PlaceObject`, detect duplicates, and have ExtractionService and every existing writer of `Place` ORM rows go through the new path.

**Independent test**: `pytest tests/core/places/test_place_object.py tests/core/places/test_repository.py tests/core/places/test_places_service.py::test_create tests/core/places/test_places_service.py::test_create_batch tests/core/extraction/` passes; `mypy --strict src/kebi/core/places/ src/kebi/core/extraction/` passes; the smoke recipe in `quickstart.md` Step 6 sections 1–3 (create, duplicate, get) succeeds.

### Implementation — new data layer (write side)

- [X] T012 [US1] Create `src/kebi/core/places/repository.py` with `PlacesRepository` class (note: PLURAL — see research.md Decision 3). Constructor takes an `AsyncSession`. Implement `_build_provider_id(provider, external_id) -> str | None` (the ONLY namespace-construction site in the whole codebase — guarded by a comment). Implement `create(data: PlaceCreate) -> PlaceObject` using `INSERT … RETURNING` via `sqlalchemy.dialects.postgresql.insert`; catch `IntegrityError` on the partial unique index and re-raise as `DuplicatePlaceError([DuplicateProviderId(provider_id, existing_place_id)])` after fetching the existing `place_id`; catch other DB errors and wrap in `RuntimeError`. Implement `create_batch(items: list[PlaceCreate]) -> list[PlaceObject]` with empty-list short-circuit, single `INSERT … RETURNING` for the whole batch in one transaction, all-or-nothing on `IntegrityError` (rollback, raise `DuplicatePlaceError` listing every conflicting `provider_id`). Implement `get(place_id) -> PlaceObject | None` and `get_by_external_id(provider, external_id) -> PlaceObject | None` and `get_batch(place_ids) -> list[PlaceObject]` (preserve order, omit missing rows). Implement an `_orm_to_place_object(row) -> PlaceObject` helper that materializes Tier 1 fields with `geo_fresh=False`, `enriched=False`, Tier 2/3 fields `None`. Depends on T005, T008.
- [X] T013 [US1] Create `src/kebi/core/places/service.py` with `PlacesService` class. Constructor takes `repo: PlacesRepository`, `cache: PlacesCache | None = None`, `client: PlacesClient | None = None` (the cache and client params accept `None` so Phase 3 can ship before Phase 4/5 — they will be required by `enrich_batch` only). Implement `create(data) -> PlaceObject` (delegates to `repo.create`), `create_batch(items) -> list[PlaceObject]` (delegates to `repo.create_batch`), `get(place_id) -> PlaceObject | None` (delegates to `repo.get`). Leave `enrich_batch` as a stub that raises `NotImplementedError("enrich_batch lands in US2/US3")` for now — Phase 4/5 fills it in. Depends on T012.
- [X] T014 [P] [US1] Update `src/kebi/core/places/__init__.py` to re-export `PlacesService`, `PlaceObject`, `PlaceCreate`, `PlaceType`, `PlaceSource`, `PlaceProvider`, `PlaceAttributes`, `LocationContext`, `GeoData`, `PlaceEnrichment`, `HoursDict`, `DuplicatePlaceError`, `DuplicateProviderId` per contracts/places-service.md, while keeping the existing `PlacesClient`, `GooglePlacesClient`, `PlacesMatchResult`, `PlacesMatchQuality` exports. Depends on T005, T013.

### Tests — new data layer (write side)

- [X] T015 [P] [US1] Create `tests/core/places/test_repository.py` with mocked `AsyncSession`. Test cases per spec.md User Story 1 + contracts/places-service.md: `create` builds `provider_id` from `provider`+`external_id`; `create` with `provider=None` or `external_id=None` stores `provider_id=None`; `create` raises `DuplicatePlaceError` on `IntegrityError` and the error carries the existing `place_id`; `create_batch([])` returns `[]` without calling `session.execute`; `create_batch([3 items])` issues exactly one `session.execute` call (assert via `mock.call_count == 1`); `create_batch` preserves input order; `create_batch` with one colliding row rolls back the transaction and raises `DuplicatePlaceError` listing the conflicts; `get_by_external_id` builds the namespaced key internally and queries `provider_id`. Depends on T012.
- [X] T016 [P] [US1] Create `tests/core/places/test_places_service.py` with mocked `repo`, `cache`, `client`. Add ONLY the create-path tests in this phase: `create` returns Tier 1 `PlaceObject` with `geo_fresh=False`, `enriched=False`; `create_batch` calls `repo.create_batch` exactly once (not N times); `create_batch` preserves input order; `create_batch([])` returns `[]` without touching the repo. (US2 and US3 add more tests to this file.) Depends on T013.

### Migration of existing writers and intermediate types

- [X] T017 [US1] Modify `src/kebi/core/extraction/types.py`: delete the `ExtractionResult` dataclass and the `CandidatePlace` dataclass (if it lives here). Update any in-file imports. Add a re-export of `PlaceCreate` and `PlaceObject` from `kebi.core.places` so callers that imported from `extraction.types` still resolve during the migration of dependent files. Depends on T005, T014.
- [X] T018 [US1] Modify `src/kebi/core/extraction/validator.py`: stop constructing `ExtractionResult`. Construct `PlaceCreate` instead. Map `address`/`lat`/`lng` (which were validator outputs) into a temporary local variable that the persistence layer will use to write the geo cache later (NOT into `PlaceCreate.attributes`); map `cuisine` → `PlaceCreate.attributes.cuisine`; map `external_provider/external_id` → `PlaceCreate.provider`+`PlaceCreate.external_id`. Update return type annotation. Depends on T017.
- [X] T019 [US1] Modify `src/kebi/core/extraction/persistence.py`: replace lines 96-110 (the direct `Place(...)` ORM constructor) with a `places_service.create_batch([...])` call. The dedup loop on `_place_repo.get_by_provider` becomes a pre-check via `places_service.get_by_external_id` (or simply catch `DuplicatePlaceError` from `create_batch` and convert each conflict into `PlaceSaveOutcome(status="duplicate", place_id=conflict.existing_place_id)`). Inject `PlacesService` via the existing dependency-injection pattern (constructor parameter). Remove the `_place_repo` field. Depends on T013, T018.
- [X] T020 [US1] Modify `src/kebi/core/extraction/handlers/extraction_pending.py`: stop reading `result.address`/`result.cuisine`/`result.confidence`/`result.external_provider`/`result.external_id`. Read from `PlaceObject.attributes.*` and `PlaceObject.provider_id` instead. Update the status payload builder to emit the new field shape. Depends on T017.
- [X] T021 [US1] Modify `src/kebi/core/extraction/service.py`: replace every `ExtractionResult` reference with `PlaceObject` (read) or `PlaceCreate` (write). The service's response becomes `list[PlaceObject]`. Depends on T017, T019.
- [X] T022 [US1] Modify `src/kebi/core/extraction/extraction_pipeline.py`: ensure every node accepts and yields `PlaceCreate`/`PlaceObject` rather than `ExtractionResult`. No state field renames beyond that. Depends on T021.
- [X] T023 [US1] Modify `src/kebi/core/extraction/dedup.py`: dedup keys are `PlaceCreate.provider`+`PlaceCreate.external_id` (for not-yet-saved places) or `PlaceObject.provider_id` (for already-saved places). Depends on T017.
- [X] T024 [P] [US1] Modify `src/kebi/core/extraction/enrichers/llm_ner.py`: keep the local `_NERPlace` dataclass (it is the LLM's structured output schema). Change the function exit so it converts `_NERPlace → PlaceCreate`, mapping `_NERPlace.cuisine → PlaceAttributes.cuisine`, `_NERPlace.price_range` → `PlaceAttributes.price_hint` via the standard low/mid/high → cheap/moderate/expensive mapping (use a shared helper). Depends on T005.
- [X] T025 [P] [US1] Modify `src/kebi/core/extraction/enrichers/whisper_audio.py`: same shape — keep `_NERPlace`, exit with `PlaceCreate`. Depends on T005.
- [X] T026 [P] [US1] Modify `src/kebi/core/extraction/enrichers/subtitle_check.py`: same shape — keep `_NERPlace`, exit with `PlaceCreate`. Depends on T005.
- [X] T027 [P] [US1] Modify `src/kebi/core/events/handlers.py`: any `PlaceSaved` event payload that referenced legacy fields now references `PlaceObject` shape. Update the handler signatures and the dispatcher payload. Depends on T005.
- [X] T028 [P] [US1] Modify `src/kebi/api/schemas/extract_place.py`: delete the `SavedPlace` Pydantic model. Update the route response model to `list[PlaceObject]`. Update any imports across the api package. Depends on T005.
- [X] T029 [US1] Modify `src/kebi/api/deps.py`: add a `get_places_service()` factory that constructs `PlacesService(PlacesRepository(session), PlacesCache(redis), GooglePlacesClient())` from the existing session and Redis dependencies. Wire it as a `Depends()`. Remove the old `get_place_repository()` factory that returned `SQLAlchemyPlaceRepository`. Depends on T013, T038 (PlacesCache lands in Phase 4), T011.

> Note: T029 references `PlacesCache` which doesn't exist until Phase 4. For Phase 3 isolation, T029 may temporarily inject `None` for `cache`/`client` — the create/get paths don't use them. Phase 7 task T072 finalizes the wiring after Phase 4 completes.

### Removal of legacy code

- [X] T030 [US1] DELETE `src/kebi/db/repositories/place_repository.py`. Confirm no remaining imports via `grep -r "from kebi.db.repositories.place_repository" src/ tests/`. Depends on T019, T029 (no remaining callers).
- [X] T031 [US1] Modify `src/kebi/db/repositories/__init__.py` to remove `PlaceRepository` and `SQLAlchemyPlaceRepository` from the imports and `__all__`. Depends on T030.
- [X] T032 [P] [US1] Modify `src/kebi/db/__init__.py` to remove any re-export of legacy place types. Depends on T030.

### Migration of existing tests for US1

- [X] T033 [P] [US1] Modify `tests/core/extraction/test_types.py`: replace `ExtractionResult`/`CandidatePlace` test fixtures with `PlaceCreate`/`PlaceObject`. Update assertions. Depends on T017.
- [X] T034 [P] [US1] Modify `tests/core/extraction/test_persistence.py`: replace the `_make_result()` factory with a `_make_place_create()` factory. Replace dedup-based assertions with `DuplicatePlaceError` assertions. Mock `PlacesService.create_batch` instead of `SQLAlchemyPlaceRepository.save`. Depends on T019.
- [X] T035 [P] [US1] Modify `tests/core/extraction/test_validator.py`: replace `ExtractionResult` assertions with `PlaceCreate` assertions. Field accesses `.address`/`.cuisine`/`.lat`/`.lng` become `.attributes.cuisine` and the temporary geo-data variable. Depends on T018.
- [X] T036 [P] [US1] Modify `tests/core/extraction/handlers/test_extraction_pending_handler.py`: replace `ExtractionResult` construction with `PlaceObject` construction. Depends on T020.
- [X] T037 [P] [US1] Modify `tests/core/extraction/enrichers/test_llm_ner.py`: keep the `_NERPlace` test case; add a new test asserting that the enricher's exit converts `_NERPlace → PlaceCreate` correctly with the price-hint mapping. Depends on T024.

**Checkpoint (US1 done)**: `pytest tests/core/places/test_repository.py tests/core/places/test_places_service.py::test_create tests/core/extraction/` passes. `mypy --strict src/kebi/core/places/ src/kebi/core/extraction/` passes. The save tool can hypothetically be wired now; the recall and consult paths still raise `NotImplementedError` from `enrich_batch`.

---

## Phase 4: User Story 2 — Recall Saved Places with Location Context (P1)

**Story goal**: a caller hands `PlacesService.enrich_batch(places, geo_only=True)` a list of saved places and gets back the same list with cached coordinates and address attached, with a clear freshness flag, and zero external provider calls.

**Independent test**: `pytest tests/core/places/test_cache.py tests/core/places/test_places_service.py::test_enrich_batch_geo_only` passes; `mypy --strict src/kebi/core/places/` passes; recall queries via the rewritten `recall_repository.py` SQL return rows that materialize cleanly into `PlaceObject`.

### Implementation — PlacesCache and recall mode

- [X] T038 [US2] Create `src/kebi/core/places/cache.py` with a single `PlacesCache` class that owns BOTH cache tiers. Constructor takes `redis: redis.asyncio.Redis`. Class constants: `GEO_PREFIX = "places:geo:"`, `ENRICHMENT_PREFIX = "places:enrichment:"`. Compute TTL lazily as `config.places.cache_ttl_days * 86400`. Implement four async methods: `get_geo_batch(provider_ids) -> dict[str, GeoData | None]` and `get_enrichment_batch(provider_ids) -> dict[str, PlaceEnrichment | None]` — each uses a single `redis.mget` call, missing keys come back as `None`, deserialize each non-`None` value via the model's `model_validate_json`. `set_geo_batch(items: dict[str, GeoData])` and `set_enrichment_batch(items: dict[str, PlaceEnrichment])` — each uses `redis.pipeline(transaction=False)` with `pipe.set(key, value.model_dump_json(), ex=ttl)` per item. All four methods short-circuit on empty input. Wrap both `set_*_batch` methods in `try/except (RedisError, ConnectionError, asyncio.TimeoutError)` to log `places.cache.write_failed` and swallow per FR-026b. On `set_enrichment_batch`, raise `ValueError` if any `PlaceEnrichment.hours` has day keys without a `timezone` key (programmer-error guard per data-model.md § 1.3). Depends on T005, T004.
- [X] T039 [P] [US2] Create `tests/core/places/test_cache.py` with a mocked `redis.asyncio.Redis`. Test cases covering BOTH tiers: `get_geo_batch` with all keys hit returns `GeoData` for each key; `get_geo_batch` with partial miss returns `None` for missing keys; `get_geo_batch([])` returns `{}` without calling Redis; `set_geo_batch` calls `pipeline.set` exactly once per item with the correct `ex=` value (`config.places.cache_ttl_days * 86400`); `set_geo_batch({})` returns immediately; `set_geo_batch` swallows `RedisError` and logs `places.cache.write_failed`. Same six tests for `get_enrichment_batch` / `set_enrichment_batch`. Plus: `hours.timezone` survives JSON serialization round-trip through `set_enrichment_batch`/`get_enrichment_batch`; `set_enrichment_batch` raises `ValueError` when day keys are present without `timezone`. Depends on T038.
- [X] T040 [US2] Implement `PlacesService.enrich_batch` GEO-ONLY path in `src/kebi/core/places/service.py`. Replace the `NotImplementedError` stub with a branch on `geo_only`: when `True`, collect unique non-null `provider_id`s, call `cache.get_geo_batch(unique_ids)` ONCE inside a `try/except (RedisError, ConnectionError, asyncio.TimeoutError)` block (on error, log `places.cache.read_failed` with `tier="geo"` and treat all as miss per FR-026a), then for each input place fan out the merged data: hits get `lat`/`lng`/`address` populated and `geo_fresh=True`; misses keep Tier 2 fields `None` and `geo_fresh=False`; `enriched` stays `False` always; `provider_id=None` places pass through unchanged at their original positions. Output preserves input order. Empty input returns `[]` immediately. Depends on T013, T038.
- [X] T041 [P] [US2] Add geo-only enrichment tests to `tests/core/places/test_places_service.py`: `enrich_batch(geo_only=True)` calls `cache.get_geo_batch` exactly once and NEVER calls `cache.get_enrichment_batch` or `client.get_place_details`; with all hits returns `geo_fresh=True` for each; with partial miss returns mixed `geo_fresh`; preserves input order; skips `provider_id=None` places; treats `RedisError` from `get_geo_batch` as "all miss" and does NOT raise; empty input returns `[]`. Depends on T040.

### Migration of recall-side readers

- [X] T042 [US2] Full rewrite of `src/kebi/db/repositories/recall_repository.py` per the **Session 2 Addendum → T042 rewritten** section above. Two modes in one repository: (a) filter mode (`query is None`) — pure `SELECT ... WHERE ... ORDER BY created_at DESC LIMIT`; (b) hybrid mode (`query is not None`) — vector + FTS on `p.search_vector` + RRF merge, with the same `WHERE` clauses applied. New types in `core/recall/types.py`: `RecallFilters` (14 optional fields) and `RecallResult` (wraps `PlaceObject` + `match_reason` + optional `relevance_score`). Delete `RecallRow` TypedDict. New method signature: `async def search(user_id, query, filters, sort_by, limit, location=None) -> tuple[list[RecallResult], int]`. Filter → WHERE mapping covers Tier 1 columns (`place_type`/`subcategory`/`source`/`created_after`/`created_before`), JSONB attribute paths (`attributes->>'cuisine'`, `attributes->'location_context'->>'city'`, etc.), and `tags @> :tags_include::jsonb`. SELECT returns Tier 1 only. `match_reason` values: `"semantic + keyword"`, `"semantic"`, `"keyword"`, `"filter"`. Separate `COUNT(*)` query for `total_count` (same WHERE, no LIMIT). `max_distance_km` is NOT a SQL clause — the `location` parameter is accepted but ignored in the repository body (handled by the recall service after `enrich_batch`). Depends on T005, T011b (the `search_vector` generated column migration MUST have landed first — the new SQL queries `p.search_vector` directly).
- [X] T043 [US2] Rewrite `src/kebi/core/recall/service.py` (or the call site if recall is implemented inline elsewhere) per the **Session 2 Addendum → T042 rewritten** §9 section above. Flow: (1) `results, total_count = await repo.search(user_id, query, filters, sort_by, limit, location)`; (2) extract places via `[r.place for r in results]`; (3) `enriched_places = await places_service.enrich_batch(places, geo_only=True)`; (4) if `filters.max_distance_km is not None and location is not None`, filter `enriched_places` in Python via haversine — drop any with `geo_fresh is False` or distance > threshold, then re-assemble `RecallResult`s preserving `match_reason` + `relevance_score` via a dict keyed by `place_id` (NOT by index, because distance filtering creates gaps); (5) return `{"results": [...], "total_count": total_count}`. Never touch `lat`/`lng` in the repository. The haversine helper lives in this service file (or `core/recall/utils.py`). Document in the service docstring: "post-distance-filter `total_count` is best-effort — it reflects the DB-level match count, not the post-geo-filter count, because we cannot cheaply know how many stale-cache places would have been dropped in other pagination windows". Depends on T040, T042.

### Migration of recall-side tests

- [X] T044 [P] [US2] Update any existing test for `recall_repository.py` (search `tests/db/repositories/` and `tests/core/recall/`) to assert against the new column shape and the `PlaceObject` return type. If no test exists yet, create `tests/db/repositories/test_recall_repository.py` with a single happy-path test that mocks the session and asserts the new SQL shape. Depends on T042.

**Checkpoint (US2 done)**: `pytest tests/core/places/test_cache.py tests/core/places/test_places_service.py tests/db/repositories/test_recall_repository.py tests/core/recall/` passes. `mypy --strict` passes on the recall surface. The recall tool can be wired now; the consult tool still raises (the full enrichment path).

---

## Phase 5: User Story 3 — Consult with Live Place Details (P1)

**Story goal**: a caller hands `PlacesService.enrich_batch(places, geo_only=False)` a small candidate set and gets back the list with location AND live details attached, fetching from Google only for the cache misses, deduping by provider_id internally, capped at `config.places.max_enrichment_batch`.

**Independent test**: `pytest tests/core/places/test_places_service.py::test_enrich_batch_full` passes; consult/taste/ranking services compile and their tests pass against the new `PlaceObject` shape.

### Implementation — full enrichment path

> Note: `PlacesCache` already exposes both `get_enrichment_batch` / `set_enrichment_batch` — that was done in T038/T039 during Phase 4. Phase 5 only adds the service-level full-enrichment path.

- [X] T045 [US3] *(placeholder — cache implementation already covered by T038; kept for numbering continuity, may be deleted by the operator).*
- [X] T046 [US3] *(placeholder — cache tests already covered by T039; kept for numbering continuity, may be deleted by the operator).*
- [X] T047 [US3] Implement `PlacesService.enrich_batch` FULL path in `src/kebi/core/places/service.py`. When `geo_only=False`: (a) collect unique non-null `provider_id`s from input; (b) call `cache.get_geo_batch` and `cache.get_enrichment_batch` once each, each in its own `try/except (RedisError, ConnectionError, asyncio.TimeoutError)` block treating errors as "all miss" per FR-026a and logging `places.cache.read_failed` with `tier` set to `"geo"` or `"enrichment"`; (c) compute `misses = (unique - geo_hits) | (unique - enr_hits)`; (d) if `len(misses) > config.places.max_enrichment_batch`, sort `misses` deterministically and slice to the cap, log `places.enrichment.fetch_cap_exceeded` with `requested`/`cap`/`dropped`; (e) issue `asyncio.gather(*[client.get_place_details(_strip_namespace(pid)) for pid in misses], return_exceptions=True)` — the ONLY namespace-parsing site in the codebase, guarded by a comment. This is ONE API call per unique miss, returning the combined geo+enrichment payload; (f) for each successful response, call `_map_provider_response` (per data-model.md § 5) to split the ONE response into `GeoData` + `PlaceEnrichment` locally in Python — no second API call; for each Exception, log `places.enrichment.fetch_failed` and skip; (g) call `cache.set_geo_batch(new_geo)` and `cache.set_enrichment_batch(new_enr)` (best-effort writeback — one API call → two cache writes); (h) merge data onto every input place by `provider_id`, set `geo_fresh=True` and `enriched=True` for places whose tiers were populated; (i) preserve input order, fan out duplicates, skip `provider_id=None` places. Depends on T040, T038, T011.
- [X] T048 [P] [US3] Add full-enrichment tests to `tests/core/places/test_places_service.py`: `enrich_batch(geo_only=False)` with all hits makes zero `client.get_place_details` calls; with partial miss calls `get_place_details` only for misses and makes EXACTLY ONE call per unique missing provider_id (not two — one call returns both tiers); uses `asyncio.gather` (assert via patching `asyncio.gather` and inspecting call args); preserves input order; skips `provider_id=None`; dedupes by `provider_id` (input with same place twice → one cache lookup, one fetch); caps misses at `config.places.max_enrichment_batch` and logs `places.enrichment.fetch_cap_exceeded`; returns `enriched=True` and `geo_fresh=True` on populated tiers; treats `RedisError` from either `get_geo_batch`/`get_enrichment_batch` as that-tier-all-miss without raising; treats one `client.get_place_details` failure as that-place-skipped without poisoning the batch; verifies that `cache.set_geo_batch` AND `cache.set_enrichment_batch` are both called after a successful fetch (one API call → two cache writes). Depends on T047.

### Migration of consult/taste/ranking/intent readers

- [X] T049 [P] [US3] Modify `src/kebi/core/taste/service.py` lines ~191-194: replace `place.price_range` and `place.ambiance` with `place.attributes.price_hint` and `place.attributes.ambiance`. The `place` parameter type annotation becomes `PlaceObject`. Depends on T005.
- [X] T050 [US3] **Re-opened for session 4** after T053 lands. The session 3 `PlaceObject` migration of this file is complete, BUT the file still reads OLD `ParsedIntent` field names (`intent.price_range`, `intent.radius`, `intent.discovery_filters["cuisine"]`, etc.). When T053 lands the new `ParsedIntent` shape, those references must be re-audited and replaced:
  - `intent.price_range` (values `low`/`mid`/`high`) → `intent.price_hint` (values `cheap`/`moderate`/`expensive`/`luxury`). **Do not map** — the new values are the source of truth; drop any low→cheap/mid→moderate/high→expensive lookup.
  - `intent.radius` → `intent.radius_m`.
  - `intent.occasion` → REMOVED. Replace every read with `intent.good_for` (list-valued). A single occasion like "date night" is now `good_for=["date-night"]`. A bare `intent.occasion` string lookup should become `"date-night" in intent.good_for` etc.
  - `intent.discovery_filters["cuisine"]` → `intent.cuisine` (top-level). Same for any other field that used to live inside the `discovery_filters` catch-all bag — those all now have dedicated slots on `ParsedIntent`.
  - `intent.discovery_filters` now carries ONLY `type` and `opennow`. Any code that reads other keys must now read them from the top-level intent fields.
  - `RecallFilters(...)` construction becomes trivial straight assignment since the field names now match on both sides: `RecallFilters(place_type=intent.place_type, cuisine=intent.cuisine, price_hint=intent.price_hint, ambiance=intent.ambiance, good_for=intent.good_for, dietary=intent.dietary, neighborhood=intent.neighborhood, city=intent.city, country=intent.country, source=intent.source, max_distance_km=(intent.radius_m / 1000 if intent.radius_m else None))`. Delete any helper function that previously did field remapping.

  Also keep the original session 3 scope of this task (PlaceObject throughout the LangGraph pipeline, `enrich_batch(geo_only=False)` at the right seam, `get_batch` positional-alignment audit) — it was already done in session 3 and should not regress. Depends on T053. Uncheck and re-check when the new intent-aware consult migration is green.
- [X] T051 [P] [US3] Modify `src/kebi/core/consult/types.py`: delete any local "place"/"candidate" dataclass. Use `PlaceObject` directly. Update all imports across `core/consult/`. Depends on T005.
- [X] T052 [P] [US3] Modify `src/kebi/core/ranking/service.py`: ranking signals come from `PlaceObject.attributes.*`; distance computations use `PlaceObject.lat/lng` from Tier 2 (gracefully handle `None` — rank without distance for places without cached location). **Audit for parallel-array patterns**: if ranking pre-computes scores indexed by input position and joins them against `get_batch(input_ids)` results, the silent-drop semantics of `get_batch` will misalign the arrays when even one input ID is missing. Either (a) re-key scores by `place_id` into a dict, (b) use `get()` per ID, or (c) compute scores from the `PlaceObject` instances directly after the batch fetch returns. Depends on T005.
- [X] T053 [US3] **Full rewrite of `src/kebi/core/intent/intent_parser.py` from scratch** around the new schema. The current code predates `PlaceObject`; it invented its own "place" vocabulary and every downstream service translated from it. Delete both `_IntentLLMOutput` AND the old `ParsedIntent` entirely. Replace with a single new `ParsedIntent` whose field names match `PlaceObject` / `PlaceAttributes` / `RecallFilters` exactly — no translation layer anywhere downstream.

  **New `ParsedIntent` shape**:
  ```python
  class ParsedIntent(BaseModel):
      """Structured intent that maps directly to RecallFilters and consult inputs.
      Every field name matches PlaceObject / PlaceAttributes exactly.
      """
      # Maps directly to RecallFilters
      place_type:      PlaceType | None = None
      subcategory:     str | None = None
      cuisine:         str | None = None
      price_hint:      str | None = None        # cheap | moderate | expensive | luxury
      ambiance:        str | None = None
      good_for:        list[str] = []           # date-night | solo | groups | etc.
      dietary:         list[str] = []
      tags_include:    list[str] = []
      neighborhood:    str | None = None
      city:            str | None = None
      country:         str | None = None
      source:          str | None = None

      # Consult-specific
      radius_m:              int | None = None
      search_location_name:  str | None = None
      search_location:       dict[str, float] | None = None  # resolved lat/lng, filled by service
      enriched_query:        str                             # always present; used by vector search + Google keyword
      discovery_filters:     dict[str, Any] = {}             # Google Places subtype hint ONLY (type, opennow); NOT for routing
  ```

  Also delete the separate `_IntentLLMOutput` — Instructor can constrain the LLM to `ParsedIntent` directly (minus the resolved `search_location` field, which the service layer fills in after geocoding).

  **System prompt rewrite**: every field reference in the prompt must use the exact vocabulary from `PlaceAttributes` — no invented synonyms, no remapping. Include at least these five examples inline in the prompt:

  ```
  Query: "cheap ramen nearby"
  → place_type: "food_and_drink", subcategory: "restaurant", cuisine: "japanese",
    price_hint: "cheap", radius_m: 500, enriched_query: "cheap ramen nearby"

  Query: "nice dinner in Sukhumvit for a date"
  → place_type: "food_and_drink", price_hint: "expensive", good_for: ["date-night"],
    neighborhood: "Sukhumvit", enriched_query: "nice dinner in Sukhumvit for a date"

  Query: "quiet museum in Tokyo for a rainy afternoon"
  → place_type: "things_to_do", subcategory: "museum", ambiance: "quiet",
    city: "Tokyo", enriched_query: "quiet museum Tokyo indoor"

  Query: "boutique hotel near the beach for a honeymoon"
  → place_type: "accommodation", subcategory: "hotel",
    good_for: ["romantic", "special-occasion"],
    enriched_query: "boutique beach hotel romantic"

  Query: "cute bookstore in Shibuya"
  → place_type: "shopping", subcategory: "bookstore", ambiance: "cozy",
    neighborhood: "Shibuya", enriched_query: "cute bookstore Shibuya"
  ```

  **Important — no food bias in the prompt body.** The system prompt must treat all 5 place types as equally first-class. When describing vocabulary, include at least one example from a non-food type alongside every food example. `cuisine` and `dietary` are correctly food-only (documented as "use only when `place_type == food_and_drink`"); `subcategory` + `good_for` + `ambiance` + `tags_include` work across all types and must be described accordingly. The old prompt framed the product as a "food and dining" recommender — this is wrong; the rewrite is a place recommender covering food, things to do, shopping, services, and accommodation equally.

  **`enriched_query` rules**:
  - Always present (non-None, non-empty).
  - If the raw query is already clear and needs no augmentation, return it verbatim.
  - If user memories are injected into the prompt and a relevant preference applies, rewrite to include that preference inline (e.g. raw "somewhere for dinner" + memory "user is vegan" → enriched "vegan dinner").
  - This field feeds **both** the recall vector search (via the embedder) and Google Places discovery (as the `keyword` parameter). It does NOT need to be grammatical — keyword density matters more than sentence structure.

  **`discovery_filters` rules**:
  - Keeps ONLY the Google Places subtype hint (`type`, `opennow`). Nothing else.
  - Does NOT carry the place type, cuisine, price, or any other field that now has a dedicated slot on `ParsedIntent`. Those fields route the recall query and the ranking weights — they are not "discovery filters".
  - The current code uses `discovery_filters` as a catch-all for everything the LLM extracted — strip it down to just `{"type": "<google_type>", "opennow": True}` at most.

  **Config changes**: remove the `config.consult.radius_defaults` nested object entirely. Replace with three flat integer keys:
  ```yaml
  consult:
    default_radius_m: 1500
    nearby_radius_m: 500
    walking_radius_m: 1000
  ```
  Update `ConsultConfig` Pydantic in `core/config.py` to match. The LLM maps proximity language directly: "nearby" → `nearby_radius_m`, "walking distance" → `walking_radius_m`, anything else → `default_radius_m` (or `None` if no signal).

  **Downstream cleanup — NO translation layer anywhere**:
  - `ConsultService` (T050) reads `intent.place_type` / `intent.cuisine` / `intent.price_hint` etc. directly. No `_intent_to_filters` helper, no field remapping dict.
  - Recall filter construction in the consult tool builds `RecallFilters` via straight assignment: `RecallFilters(place_type=intent.place_type, cuisine=intent.cuisine, price_hint=intent.price_hint, good_for=intent.good_for, dietary=intent.dietary, neighborhood=intent.neighborhood, city=intent.city, country=intent.country, ...)`. Same field names on both sides = trivial mapping.
  - If any existing code does `filters["price_range"] = intent.price_range` or `intent.discovery_filters["cuisine"]` style remapping — delete it. Every field name now matches end to end.
  - `search_location_name` remains as a raw LLM capture for observability (e.g. "Sukhumvit", "Asok BTS"). `search_location` is the resolved `{"lat": float, "lng": float}` the geocoder produces. This split is unchanged from the current code.

  **Tests**: rewrite `tests/core/intent/test_intent_parser.py` (if it exists) around the new schema. The five prompt examples above become unit tests asserting the expected `ParsedIntent` shape. Also test: `enriched_query` is always non-None; `discovery_filters` never carries fields that have a dedicated slot; memory injection rewrites `enriched_query` correctly.

  **⚠️ Downstream ripple**: this rewrite invalidates any code path that currently does `intent.price_range` or `intent.discovery_filters["cuisine"]` or similar. T050 (`ConsultService`) and any route that passes `ParsedIntent` forward all need to be updated in the same commit. Do not land T053 without landing the downstream consumers in the same PR.

  Depends on T005 (for `PlaceType` enum import).

  **⚠️ Re-open T050 and T055 when T053 lands.** T050 (ConsultService migration) was marked complete BEFORE T053 was upgraded to a full rewrite. The current T050 code reads the OLD `ParsedIntent` field names (`intent.price_range`, `intent.radius`, `intent.discovery_filters["cuisine"]`, etc.). When T053 lands, those references will either fail mypy or silently read None from missing attributes. The session executing T053 MUST also:
   - Re-audit `src/kebi/core/consult/service.py` for every `intent.*` reference and replace with the new field names (`intent.price_hint`, `intent.radius_m`, `intent.cuisine`, etc.).
   - Re-audit any `RecallFilters(...)` construction in the consult flow — with the new schema, every field assigns by identical name (`RecallFilters(place_type=intent.place_type, cuisine=intent.cuisine, price_hint=intent.price_hint, ...)`). Delete any remapping dict or helper function that existed before.
   - Re-run `tests/core/consult/`, `tests/core/chat/`, and any other test that constructs a `ParsedIntent` fixture — update every fixture to the new shape.
   - Uncheck T050 and T055 in `tasks.md` before starting, then re-check them when the new intent-aware consult migration is green.

### Migration of consult/taste/ranking tests

- [X] T054 [P] [US3] Modify `tests/core/chat/test_service.py` lines ~51-62: replace `SavedPlace` construction with `PlaceObject` construction. Update field assertions accordingly. Depends on T028.
- [X] T055 [US3] **Re-opened for session 4** after T053 lands. The `PlaceObject` fixture migration is complete from session 3, but any test that constructs a `ParsedIntent` fixture (search `tests/core/consult/`, `tests/core/intent/`, `tests/core/chat/` for `ParsedIntent(...)` calls) uses the OLD field names (`occasion`, `price_range`, `radius`, etc.) and will fail validation against the new schema. Update every `ParsedIntent` fixture to the new field names (`good_for`, `price_hint`, `radius_m`, etc.). Also add five new unit tests in `tests/core/intent/test_intent_parser.py` (create if missing) — one per prompt example in T053, asserting the expected `ParsedIntent` field values end-to-end. Depends on T053 and T050. Uncheck and re-check when the test suite is green.
- [X] T056 [P] [US3] Modify `tests/core/extraction/test_dedup.py`: replace `CandidatePlace.cuisine` with `PlaceCreate.attributes.cuisine` (or `PlaceObject.attributes.cuisine`, depending on which side runs the dedup). Depends on T023.

**Checkpoint (US3 done)**: `pytest tests/core/places/ tests/core/consult/ tests/core/taste/ tests/core/ranking/` passes. `mypy --strict` passes across the entire `src/kebi/core/` tree. Every reader and writer of place data in the app uses `PlaceObject` / `PlaceCreate`.

---

## Food-bias audit findings (added 2026-04-15, must land in session 4 Block 1)

A cross-file audit surfaced five remaining spots where the old restaurant-recommender mental model still leaks through — code that was migrated to `PlaceObject` at the shape level but still hardcodes food assumptions in behavior or prompts. These are additions to Block 1, BEFORE Phase 6 runs. Each has a dedicated task below:

- **T050a** — `core/consult/types.py` `map_google_place_to_place_object()` hardcodes `place_type=PlaceType.food_and_drink`. A hotel, museum, or boutique discovered via Google will be labeled as food and rank/respond wrong. Fix: infer `place_type` from Google's `types[]` array — `lodging` → `accommodation`; `museum`/`park`/`tourist_attraction`/`aquarium`/`zoo` → `things_to_do`; `store`/`shopping_mall`/`book_store`/`clothing_store` → `shopping`; `restaurant`/`cafe`/`bar`/`bakery` → `food_and_drink`; else `services`. Add a small mapper `_google_types_to_place_type(types: list[str]) -> PlaceType` next to the function; default to `services` when no mapping matches and log `google_place_type_unknown` with the raw `types` list.
- **T050b** — `core/chat/router.py` line 18 system prompt reads "You are an intent classifier for a **food and dining app**" and every classification example (`"RAMEN KAISUGI"`, `"best pizza"`, `"cheap dinner"`, `"pad see ew vs pad thai"`) is food. Reframe as "You are an intent classifier for a **place recommendation app**" and add at least 2 non-food examples to each classification bucket — extract-place (`"The Louvre is amazing"`, `"this hotel in Ubud is beautiful"`), consult (`"nice boutique hotel near the beach"`, `"things to do with kids in Kyoto"`), assistant-fallback (`"are museums free on Sundays?"`).
- **T050c** — `core/extraction/enrichers/llm_ner.py` lines 129-130 user prompt says "Extract all real venue names (restaurants, cafes, bars, shops, attractions)". Food-first list; "attractions" is vague. Rewrite to give equal weight across types: "Extract all real venue names (restaurants, cafes, bars, hotels, hostels, museums, parks, markets, shops, galleries, co-working spaces, and similar places)." Update the corresponding `_NERPlace` schema docstring to note that `cuisine` is optional and only populated for food places.
- **T050d** — `scripts/seed_migration.py` lines 63-80 keyword ladder has 14 keywords for `things_to_do` and zero for `shopping`/`accommodation`/`services`. Legacy rows lacking both `cuisine` and a `things_to_do` keyword silently fall through to `services`, which mislabels half of them. Add three new keyword sets: `_SHOPPING_KEYWORDS = {"mall", "boutique", "market", "store", "bookstore", "shop"}`, `_ACCOMMODATION_KEYWORDS = {"hotel", "hostel", "resort", "inn", "ryokan", "guesthouse", "rental"}`, `_SERVICES_KEYWORDS = {"gym", "spa", "salon", "barber", "coworking", "pharmacy", "clinic", "laundry"}`. Rewrite `infer_place_type()` to check all four keyword sets in parallel; if multiple match, prefer the set with more hits in the name; else fall through to the cuisine check; else default to `services` with the existing `place_type_defaulted` log line. Re-run the unit test block at the bottom of the script to cover museum, hotel, bookstore, gym inputs.
- **T050e** — T055 re-audit must also extend test fixtures to all 5 `PlaceType` values (not just `food_and_drink`). Add one `@pytest.mark.parametrize` over `PlaceType` to each of `test_places_service.py::test_create`, `test_repository.py::test_create_persists_tier1_fields`, and any similar "happy path" test that today hardcodes `PlaceType.food_and_drink`. This is a single-line addition per test and catches any latent place-type-specific bug that would otherwise ship.

These five tasks run AFTER T053/T050/T055 land and BEFORE Phase 6 starts. Together they take Block 1 from "mypy green, tests green" to "mypy green, tests green, system actually recommends non-food places with equal quality".

---

## Session 4 ordering constraint

Session 4 is the final session. It is the ONLY session that will execute. It has three blocks of work that MUST run in this order:

1. **Intent parser rewrite + downstream re-audit + food-bias cleanup** (T053 → T050 → T050a → T050b → T050c → T050d → T055 → T050e). Do this FIRST. T053 changes `ParsedIntent` shape; T050 and T055 regress against the new schema and must be re-audited; the five T050a–T050e tasks fix residual food-centric assumptions that the session-3 migration shipped without. Get mypy + pytest green on `core/intent/`, `core/consult/`, `core/chat/`, `core/extraction/enrichers/`, `scripts/`, `tests/core/consult/`, `tests/core/intent/`, `tests/core/chat/`, `tests/core/places/`, `tests/core/extraction/enrichers/` before moving on.
2. **Phase 6 — DB migration verification** (T057 → T058 → T059 → T060 → T061 → T062). Requires a running local Postgres + Redis. The user has granted explicit permission to run `python scripts/seed_migration.py` and `alembic upgrade head` in session 4 — this is the final session so the DB work must happen.
3. **Phase 7 — Polish + final commit** (T063 → T064 → T065 → T066–T072 parallel → T073). Includes fixing the 4 pre-existing mypy errors in `places_client.py` and the 9 pre-existing pytest failures (5 in `test_places_client.py` + 4 in `test_chat_assistant_service.py`) flagged by session 3. These are not strictly feature 019 bugs but they block T063/T065's green gate, so they land here.

Blocks 1 and 2 must both be green before Phase 7 starts. Block 3 ends with a single feature commit on `019-places-service`; no push to `main`.

---

## Phase 6: User Story 4 — Migrate Existing Place Records Without Data Loss (P2)

**Story goal**: an operator runs the seed migration script against a populated database, then `alembic upgrade head`, and no place loses its identity, name, type, source, or relocated data. Gracefully handles rows that cannot be relocated.

**Independent test**: with a populated copy of the DB containing legacy `cuisine`/`price_range`/`lat`/`lng` rows, run `python scripts/seed_migration.py` then `alembic upgrade head`; verify that (a) every row's `cuisine` is in `attributes.cuisine`, (b) every row's `price_range` is in `attributes.price_hint` (or logged as unmapped), (c) Redis `places:geo:{provider_id}` exists for rows that had a `provider_id` plus geo data, (d) every row has a non-null `place_type`, (e) the `seed_migration.log` file lists every defaulted row.

- [X] T057 [US4] Create or refresh a seedable test database snapshot: dump the current `dev` DB (or generate a synthetic one if `dev` is empty) with at least 20 legacy rows representative of the data — at least 5 with `cuisine`, 5 with `price_range`, 5 with `lat`/`lng`/`address`+`provider_id`, 3 with `lat`/`lng`/`address` but NO `provider_id`, 3 with no relocatable data. Document the snapshot in `specs/019-places-service/test-fixtures/legacy_places_seed.sql`. Depends on Phase 2 complete.
- [X] T058 [US4] Run `python scripts/seed_migration.py` against the seedable database. Verify the stdout report counts match expectations (cuisine relocated ≥ 5, price_range mapped ≥ 5 of which 0 unmapped if all values are in `low/mid/high`, geo cache seeded ≥ 5, geo data lost = 3, place_type inferred = 20). Verify `scripts/seed_migration.log` contains `place_type_defaulted` lines for any row that fell through to the default. **Confirm the operator-review gate fires**: if the test fixture has any defaulted rows, the script must exit with code 2 and print "REVIEW REQUIRED". Then either manually re-classify the defaulted rows via `psql` UPDATE statements, OR re-run with `--accept-defaults`. Verify the second run exits 0 and writes `accepted_defaults` to the log. Depends on T057.
- [X] T059 [US4] Run `poetry run alembic upgrade head` against the same database. Verify no errors, then connect via `docker compose exec -T postgres psql -U postgres -d kebi -c "\\d+ places"` and confirm the column list matches `data-model.md` § 2.1. Confirm `uq_places_provider_id` partial unique index exists. Confirm the legacy columns are GONE. Depends on T058.
- [X] T060 [US4] Verify Redis state: `docker compose exec -T redis redis-cli KEYS 'places:geo:*' | wc -l` should equal the "geo cache seeded" count from T058. Spot-check one key with `redis-cli GET` and confirm the JSON payload deserializes into `GeoData` (use a Python REPL one-liner). Confirm TTL is approximately `30 * 86400` seconds. Depends on T059.
- [X] T061 [US4] Test the rollback path: `poetry run alembic downgrade -1` should run without error against the migrated database. Note: rollback restores the legacy columns as nullable (the seed script's relocations into JSONB are NOT reverted — JSONB stays in `attributes`). Document this asymmetry in a comment in the migration file's `downgrade()` function. Depends on T059.
- [X] T062 [US4] Re-run the seed migration script to verify idempotency: counts should be 0 on the second run (everything already relocated), no errors raised, no log lines emitted for already-relocated fields. Depends on T058.

**Checkpoint (US4 done)**: a populated database can be migrated end-to-end without data loss; the migration is idempotent; the rollback path works for the schema (with the documented JSONB exception).

---

## Phase 7: Polish and Cross-Cutting Concerns

**Purpose**: final wiring, full-suite verification, doc updates, Bruno collection sync, agent context refresh. No story label.

- [X] T063 [P] Run `poetry run mypy --strict src/kebi/` against the entire source tree and fix every reported error. The reshape of `Place` ORM in T008 will surface every remaining legacy field reference; this task closes them.
- [X] T064 [P] Run `poetry run ruff check src/ tests/` and `poetry run ruff format src/ tests/`. Fix or accept all fixes.
- [X] T065 Run `poetry run pytest` (full suite) and verify all tests pass. Failures here mean a test that was missed in Phases 3–5; fix in place.
- [X] T066 [P] Update any `.bru` file in `kebi-config/bruno/` whose example response references the legacy `address`/`cuisine`/`price_range`/`external_provider`/`external_id` fields. Replace with the `PlaceObject` shape. Search via `grep -lr "external_provider\|cuisine\|price_range" kebi-config/bruno/`.
- [X] T067 [P] Run `.specify/scripts/bash/update-agent-context.sh claude` to refresh `CLAUDE.md` with the final state of feature 019.
- [X] T068 [P] Add a single line to `MEMORY.md` (auto memory) under "What Is Built" pointing at the new module: `- core/places/{models, repository, cache, service}.py — PlacesService data layer (feat 019)`. Update the existing `project_built_state.md` if it lists the legacy `db/repositories/place_repository.py` entry — remove it.
- [X] T069 Walk through `specs/019-places-service/quickstart.md` end-to-end on a fresh local checkout: `poetry install`, `docker compose up -d`, `python scripts/seed_migration.py`, `alembic upgrade head`, run the Step 6 smoke recipe, verify Redis keys per Step 7. Capture any failures and fix.
- [X] T070 [P] Verify that `core/places/repository.py` is the ONLY file containing `f"{provider}:{external_id}"` or any equivalent provider-namespace construction. Run `grep -rn 'provider.*:.*external_id\|external_id.*:.*provider' src/ --include="*.py"` and confirm only `repository.py` matches. Add a comment to the helper marking it as the single construction site.
- [X] T071 [P] Verify that `core/places/service.py` is the ONLY file containing `.split(":", 1)` or equivalent provider-namespace parsing. Run `grep -rn 'split.*":"' src/kebi/core/ --include="*.py"` and confirm only `service.py` matches inside the `_strip_namespace` helper.
- [X] T071a [P] **`get_batch` positional-alignment audit**. Run `grep -rn 'get_batch' src/kebi/ --include="*.py"` and `grep -rn 'zip.*get_batch\|get_batch.*zip' src/kebi/ --include="*.py"`. For every call site, manually verify that the caller does NOT assume `len(output) == len(input)` and does NOT positionally join the result against any parallel array indexed by input position. If any site does, fix it per the guidance in plan.md § Wiring blast radius — `get_batch` silent drop (use per-ID `get()` or re-key by `place_id` into a dict). Document the audit result in a comment block at the top of `core/places/service.py`'s `get_batch` method body.
- [X] T072 Finalize the dependency wiring in `src/kebi/api/deps.py` (the placeholders from T029): `get_places_service()` now constructs `PlacesService(PlacesRepository(session), PlacesCache(redis), GooglePlacesClient())` with all three dependencies real and non-None. Confirm via the smoke recipe in T069 that consult and recall paths work.
- [X] T073 Final commit: `feat(places): introduce PlacesService data layer and migrate all readers/writers to PlaceObject`. Reference ADR-054 in the commit body. Per `.claude/rules/git.md`, scope is `places`. Do NOT push to `main`.

---

## Dependencies

### Phase ordering

```
Phase 1 (Setup)
   │
   ▼
Phase 2 (Foundational) ───── BLOCKS ALL USER STORIES
   │
   ▼
Phase 3 (US1: Save) ────────┐
   │                        │
   ▼                        │
Phase 4 (US2: Recall) ──────┤  Each US phase is independently testable
   │                        │  but US2 and US3 build on US1's PlacesService scaffold
   ▼                        │
Phase 5 (US3: Consult) ─────┤
   │                        │
   ▼                        │
Phase 6 (US4: Migrate) ─────┤  Validates the schema/data work from Phase 2
   │                        │
   ▼                        │
Phase 7 (Polish) ◄──────────┘
```

### Critical-path notes

- **T008 (reshape ORM)** is the breaking change. After it lands, mypy fails everywhere until the migration tasks in Phases 3–5 land. This is by design — the type checker is the safety net for the migration.
- **T013 (PlacesService scaffold)** must land before any `enrich_batch` work in Phases 4 and 5; the create-only stub in Phase 3 lets US1 ship without US2/US3.
- **T029 (api/deps.py)** has a forward reference to T038 and T045 (the cache classes) but ships in Phase 3 with `None` placeholders. T072 in Phase 7 finalizes it.
- **Phase 6 (US4)** depends on the seed migration script (T010) and the Alembic file (T009) from Phase 2 being correct. It does NOT depend on Phases 3–5 — you could in principle test the migration before any code migration lands. In practice, run Phase 6 last so you only run the migration once.

### Within-phase parallelism

| Phase | Parallel groups |
|---|---|
| Phase 1 | T003 ‖ T004 (after T002 lands) |
| Phase 2 | {T005, T006, T011} ‖ T008 → T009 → T010 |
| Phase 3 | {T015, T016} after T012/T013; {T024, T025, T026, T027, T028, T032} concurrent file edits; {T033, T034, T035, T036, T037} concurrent test edits |
| Phase 4 | {T039, T041, T044} parallel where they don't share files |
| Phase 5 | {T046, T048} parallel; {T049, T051, T052, T053} concurrent file edits; {T054, T055, T056} concurrent test edits |
| Phase 6 | T057 → T058 → T059 → {T060, T061, T062} |
| Phase 7 | {T063, T064, T066, T067, T068, T070, T071} all parallel; T065 after them; T069 after T072 |

---

## Implementation strategy — incremental delivery

### MVP scope

**The minimum to demonstrate value**: Phases 1, 2, and 3 (US1 only). This delivers a working `PlacesService.create()` / `create_batch()` / `get()` path, the new `Place` ORM, the migration scaffold, the seed script (untested against real data), and a fully migrated extraction pipeline. The save tool can be wired immediately after MVP. Recall and consult continue to raise `NotImplementedError` from `enrich_batch` until Phases 4 and 5 land.

### Incremental order

1. **MVP (Phases 1–3)** — save path complete; ExtractionService migrated; tests green for the write side.
2. **+ Recall (Phase 4)** — Tier 2 cache and `enrich_batch(geo_only=True)`; `recall_repository` SQL rewritten; recall tool can be wired.
3. **+ Consult (Phase 5)** — Tier 3 cache and full `enrich_batch`; consult/taste/ranking migrated; consult tool can be wired.
4. **+ Migration verification (Phase 6)** — exercise the schema/data migration on a populated DB, confirm idempotency and rollback.
5. **Polish (Phase 7)** — full mypy, ruff, pytest, Bruno, agent context, final wiring.

Each increment is independently testable per the spec's "independent test" promises. Each is a deployable state — the worst case after step 1 is "save works, recall and consult raise on enrich" which is a clear and visible failure mode rather than a silent regression.

### Risk hot spots

- **T008 → T030 sequence** is the longest critical path. Reshape ORM, then migrate every writer, then delete the legacy repo. mypy stays red for the duration; do not commit until the sequence completes locally.
- **T009 (Alembic autogen)** typically produces a draft that needs hand-editing for column order, partial unique index syntax, and the FTS index. Plan ~30 min for the edit.
- **T042 (recall SQL rewrite)** is a hand-written query; manually validate the result shape against `PlaceObject` materialization.
- **T058 / T010 (seed migration)** is the only task that touches a real database. Run on a clone first, never on `dev` until verified.

---

## Validation checklist

- [x] Every task has a checkbox (`- [ ]`), a sequential ID (T001–T073), and a file path (or a clear shell command for verification tasks).
- [x] Every Phase 3–6 task carries a `[USx]` story label.
- [x] Phase 1, Phase 2, and Phase 7 tasks have NO story label.
- [x] `[P]` markers appear only on tasks that touch different files and have no incomplete dependency.
- [x] Each user story phase ends with a "Checkpoint" line stating the independently testable end state.
- [x] Every file in plan.md § Code Migration Manifest appears as a task target.
- [x] Every test file listed in spec.md Step 10 appears as a task target.
- [x] ADRs referenced (054, supersedes 041) are added in T002 before any code touches them.
- [x] The single namespace-construction site (T070) and single namespace-parsing site (T071) are verified by explicit polish-phase tasks.

---

## Task counts

| Phase | Tasks | Notes |
|---|---|---|
| Phase 1 (Setup) | 4 | T001–T004 |
| Phase 2 (Foundational) | 7 | T005–T011 |
| Phase 3 (US1 — Save) | 26 | T012–T037 |
| Phase 4 (US2 — Recall) | 7 | T038–T044 |
| Phase 5 (US3 — Consult) | 12 | T045–T056 |
| Phase 6 (US4 — Migrate) | 6 | T057–T062 |
| Phase 7 (Polish) | 12 | T063–T073 + T071a |
| **Total** | **74** | |

| Story | Tasks |
|---|---|
| US1 (Save) — P1 | 26 |
| US2 (Recall) — P1 | 7 |
| US3 (Consult) — P1 | 12 |
| US4 (Migrate) — P2 | 6 |

| Parallel opportunities (top of each phase) | Count |
|---|---|
| Phase 2 setup tasks | 4 [P] |
| Phase 3 in-file edits across extraction enrichers / events / schemas / db init | 6 [P] |
| Phase 3 test migrations | 5 [P] |
| Phase 5 in-file edits across consult/taste/ranking/intent | 4 [P] |
| Phase 5 test migrations | 3 [P] |
| Phase 7 polish | 7 [P] |
