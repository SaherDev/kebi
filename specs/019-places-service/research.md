# Phase 0 — Research & Decisions

**Feature**: 019-places-service
**Date**: 2026-04-14
**Purpose**: Resolve the Constitution Check violations and open architectural decisions surfaced in `plan.md`. Each decision below must be acknowledged by the user before `/speckit.tasks` runs.

---

## Decision 1 — Supersede ADR-041 with strict-create + `DuplicatePlaceError`

**Decision**: Write a new ADR-054 ("PlacesService strict-create with explicit duplicate-detection lookup, supersedes ADR-041") and accept it before code lands.

**Rationale**:
- The user explicitly chose strict-create at `/speckit.clarify` Q3 ("`create()` raises `DuplicatePlaceError` ... does NOT upsert").
- ADR-041's upsert was made when the only writer was `ExtractionService.persistence`, which extracts places from one user's content at a time. The new save tool will accept manual saves, link-shares, and bulk extractions from the same user touching the same place over time. Silent overwrite would let a low-confidence TikTok extraction clobber a manual save.
- Strict-create + a separate `get_by_external_id()` lookup pushes idempotency to the caller, where the intent is visible. Callers that *want* upsert can do `lookup → if exists, decide → else create`, three lines, explicit.
- The `DuplicatePlaceError` payload exposes the existing `place_id`, so callers don't need a second round trip to recover.

**Alternatives considered**:
- *Keep ADR-041's upsert in `create()`*: rejected — silently merges/overwrites and removes the caller's chance to see a collision.
- *Add a separate `upsert()` method alongside `create()`*: rejected — proliferates surface area and lets the upsert codepath leak back into the agent tools without forcing them to think about it. Better for the data layer to be opinionated about strict-create and let the small amount of caller code that wants merge semantics own that responsibility.
- *Make `create()` return `tuple[PlaceObject, bool]` (created, existed)*: rejected — Python convention favors raising on exceptional paths, and this breaks the unified return type promise.

**ADR-054 draft text** (for `docs/decisions.md`, to be added before tasks land):

> ## ADR-054: PlacesService strict-create with explicit duplicate-detection lookup
> **Date:** 2026-04-14
> **Status:** accepted (supersedes ADR-041)
> **Context:** The original `places` schema used a composite `(external_provider, external_id)` unique key with upsert semantics on `SQLAlchemyPlaceRepository.save()`. Upsert hides intent at the data layer and was made when the only caller was the extraction pipeline. The PlacesService data layer (feature 019) introduces three callers (save, recall, consult) and the save tool needs to detect collisions explicitly so manual saves are not silently overwritten by background extractions.
> **Decision:** Replace the composite `(external_provider, external_id)` columns with a single namespaced `provider_id` column on the `places` table. The format is `"{provider}:{external_id}"`, constructed only inside the `PlacesRepository` (never elsewhere). A partial unique index enforces that any non-null `provider_id` is unique across the table. `PlacesRepository.create()` raises `DuplicatePlaceError` (with the existing `place_id` attached) on collision instead of upserting. `PlacesRepository.create_batch()` runs in a single transaction and raises `DuplicatePlaceError` (listing every conflicting `provider_id`) if any row collides — partial inserts are not permitted. Callers wanting idempotency call `get_by_external_id(provider, external_id)` first and decide what to do.
> **Consequences:** ADR-041's upsert semantics and composite-key field naming are superseded. The legacy `SQLAlchemyPlaceRepository` in `src/kebi/db/repositories/place_repository.py` remains as a temporary shim (see ADR-055 below) until ExtractionService is migrated to `PlacesRepository` in a follow-up feature. NestJS does not read `external_provider` or `external_id`, so no product-side coordination is needed. The migration relocates the values via the seed script before the columns are dropped (see data-model.md).

**User action required**: confirm ADR-054 before `/speckit.tasks`.

---

## Decision 2 — Coexistence with ExtractionService during the transition (the only real conflict in the brief)

> **SUPERSEDED 2026-04-14 by user override**: "one place object shared everywhere; none of the service uses different shape or different attributes." The phased two-revision rollout is rejected. Feature 019 now ships a single Alembic revision that adds new columns, runs the seed migration, **and** drops the legacy columns in the same migration. Every existing reader/writer of the legacy fields (`ExtractionService`, `RecallService`, `taste/service.py`, `consult/service.py`, `ranking/service.py`, `intent/intent_parser.py`, `events/handlers.py`, `api/schemas/extract_place.py`, `api/deps.py`, plus all corresponding tests) is migrated in the same PR. The full migration manifest is in `plan.md` § Code Migration Manifest. ADR-055 below is **rescinded** before it lands. The brief's "Do not modify ExtractionService" instruction is overridden by the user.
>
> The remainder of this section is preserved as historical context for why the alternative was considered.

**Decision (RESCINDED)**: Phase the schema migration in **two Alembic revisions**, not one. The first revision lands in this feature; the second lands in a follow-up feature after ExtractionService is migrated. Document this as **ADR-055**.

**Why it is needed**: The brief is internally inconsistent. It says "Do not modify ExtractionService" *and* "drop `address`, `lat`, `lng`, `cuisine`, `price_range`, `confidence`, `validated_at`, `external_provider`, `external_id`". Those columns are written directly by `src/kebi/core/extraction/persistence.py:96-110`:

```python
place = Place(
    id=place_id,
    user_id=user_id,
    place_name=result.place_name,
    address=result.address or "",       # NOT NULL today, will crash if dropped
    cuisine=result.cuisine,
    price_range=None,
    lat=result.lat,
    lng=result.lng,
    source_url=None,
    external_provider=result.external_provider or "unknown",
    external_id=result.external_id,
    confidence=result.confidence,
    source=result.resolved_by.value,
)
```

If we drop those columns in this feature, the next extraction request crashes at runtime. If we don't drop them, the brief's "zero Google content in PostgreSQL" promise is violated. Both can't be true; one must give in this PR.

**Two-revision rollout** (proposed):

**Revision A** — *this feature, `019-places-service`*:
1. **Add new columns** alongside the legacy ones: `place_type`, `subcategory`, `tags JSONB`, `attributes JSONB`, `source` (new — replaces the existing `source: String`), `provider_id` (the namespaced single column).
2. **Make legacy columns nullable** (`address`, `cuisine`, `price_range`, `lat`, `lng`, `external_provider`, `external_id`, `confidence`, `validated_at`, `ambiance`, `photo_url`, `hours`, `rating`, `phone`, `popularity`). `place_name` and `id` stay required.
3. **Backfill `provider_id`** from existing `(external_provider, external_id)` pairs where both are present: `provider_id = external_provider || ':' || external_id`. Add the partial unique index on `provider_id`.
4. **Add the user/type composite indexes and the FTS index** on `(place_name, subcategory)`.
5. **Run the seed migration script** (`scripts/seed_migration.py`) — relocates `cuisine`/`price_range` into `attributes` JSONB, seeds Redis `places:geo:{provider_id}` from rows with location data, logs everything that could not be relocated.
6. **Do NOT drop any legacy column in revision A.** ExtractionService continues to write to them via the legacy `SQLAlchemyPlaceRepository`. The new `PlacesService.create()` writes only the new columns, and reads through the new model don't see the legacy ones.

**Revision B** — *follow-up feature, e.g. `020-extraction-on-placesservice`*:
1. Migrate `ExtractionService.persistence` to call `PlacesService.create()` / `create_batch()` instead of `SQLAlchemyPlaceRepository.save()`.
2. Delete `src/kebi/db/repositories/place_repository.py` (and its tests).
3. Drop the legacy columns from `places` in a second Alembic revision: `address`, `cuisine`, `price_range`, `lat`, `lng`, `external_provider`, `external_id`, `confidence`, `validated_at`, `ambiance`, `photo_url`, `hours`, `rating`, `phone`, `popularity`.
4. Drop the composite uniqueness `uq_places_provider_external` (now redundant — replaced by the partial unique on `provider_id` from revision A).

**Why two revisions and not one**:
- A single revision would force ExtractionService to be modified inside this feature, breaking the brief's "Do not modify ExtractionService" instruction.
- A single revision would also require simultaneous deploys of the new ExtractionService persistence path and the schema change, which is risky for a non-trivial refactor.
- Splitting lets us ship the new data layer + tests + ADRs first, then sequence the ExtractionService migration on its own branch where it can be reviewed in isolation.

**The cost of phasing**: PostgreSQL has the legacy columns sitting alongside the new ones for one release cycle. They are nullable shadows; new writes via `PlacesService` ignore them; legacy writes via `ExtractionService` populate them. There is no read path in the new code that touches them.

**ADR-055 draft text (RESCINDED — DO NOT WRITE TO `docs/decisions.md`)**:

> ## ADR-055: PlacesService schema rollout in two Alembic revisions [RESCINDED]
> *(historical — superseded by user override 2026-04-14 before landing)*

**Replacement decision (post-override)**:

- **One Alembic revision** in this feature, named `places_service_schema`. It (a) adds the new columns, (b) backfills `provider_id` and `attributes` JSONB from existing data, (c) creates the new indexes, (d) drops the legacy columns and the legacy composite unique constraint — all in a single `op.batch_alter_table('places')` block where possible.
- **`scripts/seed_migration.py` runs before `alembic upgrade head`**. The migration file's header comment says so. The script is unchanged from data-model.md §4: it relocates `cuisine`/`price_range`/`ambiance` into `attributes` JSONB and seeds `places:geo:{provider_id}` from rows with location data. Without this step, the column drop would lose information.
- **Every legacy reader/writer is migrated in the same PR**. See plan.md § Code Migration Manifest for the file-by-file list. The `SQLAlchemyPlaceRepository` is deleted. ExtractionService writes via `PlacesService.create_batch()`. RecallService's hybrid SQL is rewritten to query the new column shape. Taste, consult, ranking, intent, events, schemas all consume `PlaceObject`.
- **No follow-up feature for this migration is needed.** Feature 019 is self-contained.

**Why the user override is the right call**:

1. The two-revision approach left the schema in an awkward shadow state for an indefinite period, making it harder to reason about which writer owned which columns.
2. The "one place object shared everywhere" rule is a much cleaner invariant for the rest of the codebase to depend on. With shadow columns, every service had to know which fields were "really" the data and which were tombstones.
3. Atomic schema changes are easier to review and roll back than phased ones.
4. The cost is a bigger PR — not a worse design.

**User action required (revised)**: none. The override is recorded; tasks can proceed against the single-revision plan.

---

## Decision 3 — Class-name collision: `PlacesRepository`, not `PlaceRepository`

**Decision**: Name the new class `PlacesRepository` (plural). Mirror `PlacesService`. Do **not** call it `PlaceRepository`.

**Rationale**:
- `src/kebi/db/repositories/place_repository.py` already exports a `PlaceRepository` Protocol used by `ExtractionService.persistence` and `core/extraction/persistence.py`. It will continue to exist throughout this feature (per Decision 2). Two classes with the same name in different modules works in Python but breaks code search ("which one does this import?"), confuses agent context loaders, and invites future merge bugs.
- Plural matches the rest of the new module (`PlacesService`, `PlacesClient`, `PlacesMatchResult`, `core/places/`).
- The brief's literal name was `PlaceRepository`. This is a small, justifiable deviation from the brief and is called out in the plan's Complexity Tracking. We carry the deviation explicitly into the data-model.md and contracts/places-service.md.

**Alternatives considered**:
- *Keep both classes named `PlaceRepository`*: rejected — invites import-shadowing bugs.
- *Rename the legacy one*: rejected — the brief says "Do not modify [extraction]", and renaming the legacy class touches its imports across `extraction/`.
- *`PlaceRepo`*: rejected — non-standard abbreviation in this repo.

**User action required**: confirm `PlacesRepository` (plural) as the new class name.

---

## Decision 4 — Read isolation contract (already chosen at `/speckit.clarify` Q1)

**Decision** (recorded in spec FR-007, FR-007a): The data layer trusts upstream callers. Reads (`get`, `get_batch`, `enrich_batch`) take only place identifiers, never a `user_id`. Writes (`create`, `create_batch`) stamp the caller-supplied `user_id` so ownership is recorded.

**No further action needed** — this is fixed in the spec and informs the contracts/places-service.md signatures.

---

## Decision 5 — Cache backend failure modes (already chosen at `/speckit.clarify` Q2)

**Decision** (recorded in spec FR-026a/b/c):
- Cache READ errors → graceful degradation (treat all as miss; in full-enrichment mode, route the affected places to the provider fetch path subject to the cap).
- Cache WRITE errors → log and swallow.
- Permanent-store errors → fatal.

**Implementation note**: in `PlacesService.enrich_batch`, wrap `cache.get_geo_batch` and `cache.get_enrichment_batch` in `try/except Exception` blocks that log the error with `extra={"tier": "geo"|"enrichment", "provider_id_count": N}` and substitute an empty `dict` as the result. Wrap `set_*_batch` calls the same way but inside `PlacesCache` itself so the service code stays clean. **Do not** use a bare `except` — catch `redis.exceptions.RedisError` and `ConnectionError` specifically, plus `asyncio.TimeoutError`. Anything else propagates.

**Tracing**: cache errors must emit a Langfuse-compatible structured log so they show up in observability dashboards. Reuse the existing `logging` setup; do not introduce a new tracing layer.

---

## Decision 6 — Within-batch dedupe (already chosen at `/speckit.clarify` Q5)

**Decision** (recorded in spec FR-029a): `enrich_batch` collects the unique set of `provider_id`s, MGETs each cache once across that set, fetches misses once via the provider client, and fans the merged data back out to every input position that referenced the key. The fetch cap counts unique identifiers, not input positions.

**Implementation sketch**:
```python
provider_ids_unique = {p.provider_id for p in places if p.provider_id is not None}
geo_hits = await cache.get_geo_batch(list(provider_ids_unique))         # one MGET
enr_hits = await cache.get_enrichment_batch(list(provider_ids_unique))  # one MGET (consult mode only)
misses = (provider_ids_unique - geo_hits.keys()) | (provider_ids_unique - enr_hits.keys())
if len(misses) > config.places.max_enrichment_batch:
    dropped = len(misses) - config.places.max_enrichment_batch
    logger.warning("places.enrichment.fetch_cap_exceeded", extra={"dropped": dropped})
    misses = set(list(misses)[:config.places.max_enrichment_batch])
fetch_results = await asyncio.gather(*[
    client.get_place_details(strip_namespace(pid)) for pid in misses
], return_exceptions=True)
# build merged_geo: dict[provider_id, GeoData] and merged_enr: dict[provider_id, PlaceEnrichment]
# write both back via cache.set_geo_batch + cache.set_enrichment_batch (best-effort)
# one API call per miss → _map_provider_response → two cache writes. No second API call.
# for each place in the original input, attach merged data by provider_id; preserve input order
```

`return_exceptions=True` is intentional: a single provider failure should not poison the whole batch (per existing edge-case behavior in the spec).

---

## Decision 7 — `HoursDict` serialization across Redis

**Decision**: Store the enrichment cache value as JSON via `PlaceEnrichment.model_dump_json()`. Read with `PlaceEnrichment.model_validate_json()`. The `HoursDict` `TypedDict` round-trips cleanly because Pydantic 2 serializes `TypedDict` via `dict[str, Any]` and revalidates on the way in.

**Pitfall avoided**: do not store `HoursDict` as a raw Python dict using `pickle`. Pickle ties the cache format to the Python version and class layout; JSON is portable, debuggable, and auditable in `redis-cli`.

**`timezone` key contract**: when any day key is present, `timezone` MUST be present. The cache module enforces this on `set_batch` by raising `ValueError` if a `PlaceEnrichment.hours` dict has day keys but no `timezone`. The check is a one-liner; failures here indicate a bug in the provider mapping code, not a runtime data issue.

---

## Decision 8 — SQLAlchemy 2.x async batch insert pattern

**Decision**: Use `INSERT … RETURNING *` via `sqlalchemy.dialects.postgresql.insert()` with `Returning(Place.__table__.columns)`. Single statement for the whole batch, preserves order via the input list.

**Rationale**:
- Native async SQLAlchemy 2.x supports `RETURNING` on PostgreSQL.
- One statement is meaningfully cheaper than N statements at the network round-trip level (matches FR-006 / SC-004).
- Preserves the inserted row's server-generated `created_at` / `updated_at` without a separate `SELECT`.

**Pitfall avoided**: `session.add_all([...])` + `session.flush()` issues N inserts under the hood (one per row) on PostgreSQL via SQLAlchemy 2.x async. Verified empirically. Use the explicit `insert(...).returning(...)` form to guarantee a single statement.

**Empty list short-circuit**: the repository's `create_batch([])` returns `[]` immediately without hitting the session. This is a one-line guard at the top of the method and matches FR-006.

**Transactional behavior**: the whole batch runs inside the session's existing transaction. If any row violates the partial unique index on `provider_id`, PostgreSQL raises `IntegrityError`; the repository catches it, parses the conflicting `provider_id`(s) out of the error message (or, more robustly, re-queries them via `get_by_external_id` after the rollback), and raises `DuplicatePlaceError` with the existing `place_id`s attached.

---

## Decision 9 — Redis `MGET` and pipelined `SET` patterns

**Decision**:
- Reads use `redis.mget(keys)` — single round trip.
- Writes use `async with redis.pipeline(transaction=False) as pipe: ...` — pipelined `SET key value EX ttl` for each item, single round trip.
- TTL units: `EX` (seconds). Compute `ttl = config.places.cache_ttl_days * 86400` once at startup and reuse for BOTH tiers — `PlacesCache` uses the same TTL for `set_geo_batch` and `set_enrichment_batch`.

**Pitfall avoided**: `transaction=True` on the pipeline wraps it in `MULTI`/`EXEC`, which serializes the whole pipeline atomically — unnecessary cost for a write-back cache. Use `transaction=False`.

**Connection management**: reuse the existing `get_redis()` dependency (per ADR-019) which returns the singleton `redis.asyncio.Redis` client. Do not open new connections per call.

**Empty-input guard**: both `get_batch([])` and `set_batch({})` return immediately without hitting Redis. One-line guard at the top of each.

---

## Decision 10 — `provider_id` namespace string, where it is constructed and where it is parsed

**Decision** (already in spec FR-002, FR-033 + brief constraints):
- **Constructed only inside `PlacesRepository`**, in exactly one helper: `_build_provider_id(provider: PlaceProvider | None, external_id: str | None) -> str | None`. Returns `f"{provider.value}:{external_id}"` if both are present, else `None`.
- **Parsed only inside `PlacesService.enrich_batch`** to strip the prefix before calling `client.get_place_details(external_id)`. Helper: `_strip_namespace(provider_id: str) -> str` — `provider_id.split(":", 1)[1]`.
- **Nowhere else** does the code touch the colon, the prefix, or the format.

**Rationale**: keeps the namespace as an internal detail of the data layer. Tests assert the helper behavior directly. Downstream code (caches, service, models) treats `provider_id` as an opaque string.

**Pitfall avoided**: `external_id` containing a literal colon. Google Place IDs are base64-ish strings starting with `ChIJ...` and never contain colons. Foursquare IDs are 24-char hex. Other providers may differ; we use `split(":", 1)` (max one split) so a colon in the external_id cannot cause data loss. If a future provider issues colon-containing IDs, we add a test and revisit.

---

## Decision 11 — Test boundaries and what to mock

**Decision**: Mock at the I/O boundary, not below.
- `test_repository.py` — mock `AsyncSession`. Assert that `session.execute()` is called once for batch writes, that the SQL contains `INSERT ... RETURNING`, and that the namespaced `provider_id` is built correctly. Do **not** mock SQLAlchemy internals — too brittle.
- `test_cache.py` — mock `redis.asyncio.Redis`. Assert that `mget` is called with the exact key list for both `get_geo_batch` and `get_enrichment_batch`, that `pipeline.set` is called with the exact `EX` value for both `set_geo_batch` and `set_enrichment_batch`, and that JSON round-trips correctly for both `GeoData` and `PlaceEnrichment`.
- `test_places_service.py` — mock `PlacesRepository`, `PlacesCache`, `PlacesClient`. Assert call counts (the spec's SC-001/002/003/004 numbers) and that `asyncio.gather` is used (one parallel batch, not N sequential awaits) by patching `asyncio.gather` and asserting it was called with the expected coroutines.
- `test_place_object.py` — pure Pydantic shape tests, no mocks. Round-trip every model through `model_dump_json()` / `model_validate_json()`.

**No real Postgres or Redis** in unit tests. The repo's existing pytest fixtures provide both via Docker compose for integration tests, but integration tests are out of scope for this feature (deferred to the follow-up feature that wires PlacesService into a route).

---

## Decision 12 — Configuration shape

**Decision**: Add the `places:` section to `config/app.yaml` under the existing top-level structure:

```yaml
places:
  cache_ttl_days: 30
  max_enrichment_batch: 10
```

Update the existing `AppConfig` Pydantic model in `core/config.py` to include a `places: PlacesConfig` subsection (typed `PlacesConfig` Pydantic model, fields `cache_ttl_days: int`, `max_enrichment_batch: int`, all with the values above as defaults so the section is optional). Both Tier 2 geo and Tier 3 enrichment use the same TTL — `PlacesCache` multiplies `cache_ttl_days` by 86400 for both `set_geo_batch` and `set_enrichment_batch`.

**Pitfall avoided**: do not read raw YAML in `PlacesService`. Always go through `get_config().places.*`.

---

## Decision 13 — Observability for fetch-cap overflow

**Decision**: When `enrich_batch` slices misses to `max_enrichment_batch`, emit a structured warning via the standard `logging` module:

```python
logger.warning(
    "places.enrichment.fetch_cap_exceeded",
    extra={
        "requested": len(misses),
        "cap": config.places.max_enrichment_batch,
        "dropped": len(misses) - config.places.max_enrichment_batch,
    },
)
```

The existing Langfuse callback handler will not see this (it only attaches to LLM calls). That is fine — fetch-cap overflow is a service-level operational signal, not an LLM trace. The repo's standard JSON logger picks it up.

---

## Decision 14 — Out-of-spec items deferred to later features

The following items are explicitly out of scope for `019-places-service` and will be tracked as follow-ups:

| Item | Deferred to |
|---|---|
| Migrate `ExtractionService.persistence` to use `PlacesRepository.create_batch()` | `020-extraction-on-placesservice` (or similar) |
| Drop the legacy columns from `places` (Alembic revision B) | Same as above |
| Wire `PlacesService` into the save tool (agent code) | Save-tool feature |
| Wire `PlacesService` into the recall tool | Recall-tool feature |
| Wire `PlacesService` into the consult tool | Consult-tool feature |
| Multi-provider routing (Foursquare fallback when Google has no match) | Future feature; the Protocol already supports it |
| HTTP route for `PlacesService` | None planned — ADR-052 routes everything through `POST /v1/chat` |

---

## Open questions for the user

Before `/speckit.tasks` runs, please confirm or override each of the following:

1. **ADR-054** (Decision 1) — Accept strict-create + `DuplicatePlaceError`, supersede ADR-041's upsert.
2. ~~ADR-055 (Decision 2)~~ — **RESOLVED 2026-04-14 by user override**. Single Alembic revision; all services migrated in feature 019. ADR-055 is rescinded; no new ADR needed for the rollout strategy.
3. **`PlacesRepository`** (Decision 3) — Accept the plural class name (deviation from brief's `PlaceRepository`) to avoid the collision with `db/repositories/place_repository.py`. *(Note: the legacy `db/repositories/place_repository.py` is now being deleted in this feature, so the collision dissolves on its own. The plural name is still recommended because it mirrors `PlacesService` / `PlacesClient`. Confirm or override.)*
4. **Seed migration tolerance** (data-model.md §4) — Accept that legacy rows lacking a `provider_id` lose their `lat`/`lng`/`address` permanently when the columns are dropped in this feature's Alembic revision. The seed script logs the count and the row IDs.

If any of those answers is "no", flag it now — the data-model.md, contracts/places-service.md, and plan.md § Code Migration Manifest are written assuming items 1, 3, and 4 are accepted.
