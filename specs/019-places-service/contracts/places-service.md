# Contract — `PlacesService` Internal API

**Feature**: 019-places-service
**Date**: 2026-04-14
**Audience**: This is the contract that the save tool, recall tool, and consult tool will code against in subsequent features. It is an in-process Python contract — there is no HTTP surface in this feature.
**Companion docs**: [plan.md](../plan.md), [research.md](../research.md), [data-model.md](../data-model.md)

---

## Module surface

```python
# src/kebi/core/places/__init__.py
from .service import PlacesService
from .models import (
    PlaceObject,
    PlaceCreate,
    PlaceType,
    PlaceSource,
    PlaceProvider,
    PlaceAttributes,
    LocationContext,
    GeoData,
    PlaceEnrichment,
    HoursDict,
    DuplicatePlaceError,
    DuplicateProviderId,
)
```

These are the only names callers import from `kebi.core.places`. The `PlacesRepository` and `PlacesCache` classes are internal (not re-exported) — callers receive a fully-wired `PlacesService` from a FastAPI `Depends(get_places_service)` factory in a follow-up feature.

---

## `PlacesCache`

A single cache class owns **both** the Tier 2 geo cache and the Tier 3 enrichment cache. Keeping them in one class keeps the Redis client reference in one place, simplifies dependency injection, and makes it impossible to mismatch TTLs across tiers.

```python
class PlacesCache:
    GEO_PREFIX        = "places:geo:"
    ENRICHMENT_PREFIX = "places:enrichment:"

    def __init__(self, redis: redis.asyncio.Redis) -> None: ...

    async def get_geo_batch(
        self, provider_ids: list[str]
    ) -> dict[str, GeoData | None]: ...

    async def set_geo_batch(self, items: dict[str, GeoData]) -> None: ...

    async def get_enrichment_batch(
        self, provider_ids: list[str]
    ) -> dict[str, PlaceEnrichment | None]: ...

    async def set_enrichment_batch(
        self, items: dict[str, PlaceEnrichment]
    ) -> None: ...
```

Both `set_*_batch` methods use a single TTL from `config.places.cache_ttl_days * 86400` seconds. Both `get_*_batch` methods short-circuit on empty input and issue exactly one `MGET` to Redis. Both `set_*_batch` methods short-circuit on empty input and issue a single pipelined `SET … EX ttl` batch with `transaction=False`. Write errors are caught inside the cache (`RedisError`, `ConnectionError`, `asyncio.TimeoutError`) and logged as `places.cache.write_failed` — they do not raise. Read errors propagate to the caller (`PlacesService.enrich_batch`) which catches them and treats the affected tier as "all miss".

---

## `PlacesService`

```python
class PlacesService:
    def __init__(
        self,
        repo: PlacesRepository,
        cache: PlacesCache,
        client: PlacesClient,
    ) -> None: ...
```

Three dependencies are injected. The service holds no state beyond references. It is safe to share across requests.

---

### `create(data: PlaceCreate) -> PlaceObject`

Create one place in the permanent store.

**Inputs**:
- `data: PlaceCreate` — see data-model.md §1.6. Validated by Pydantic on construction.

**Behavior**:
1. Calls `repo.create(data)`.
2. Returns the resulting `PlaceObject` with `geo_fresh=False`, `enriched=False`, and all Tier 2/3 fields `None`.
3. Does NOT write the geo cache. (The save tool writes the geo cache itself after Google validation, in a follow-up feature.)

**Raises**:
- `DuplicatePlaceError` — if the `provider_id` (built from `data.provider` + `data.external_id`) already exists. The error's `.conflicts` list contains exactly one `DuplicateProviderId` with the existing `place_id`.
- `RuntimeError` — on any other database failure (wraps `sqlalchemy.exc.SQLAlchemyError`). Permanent-store errors are fatal per FR-026c.

**Order/idempotency**:
- Not idempotent. Calling twice with the same `provider_id` raises `DuplicatePlaceError` on the second call.
- Calling with `provider=None` or `external_id=None` always succeeds (no uniqueness check).

---

### `create_batch(items: list[PlaceCreate]) -> list[PlaceObject]`

Create many places in one transaction.

**Inputs**:
- `items: list[PlaceCreate]` — may be empty.

**Behavior**:
1. If `items == []`: returns `[]` immediately. No DB call. (FR-006.)
2. Calls `repo.create_batch(items)` which executes one `INSERT … RETURNING` statement inside a single transaction.
3. Returns the resulting `PlaceObject`s in the same order as `items`.
4. All returned places have `geo_fresh=False`, `enriched=False`, Tier 2/3 fields `None`.
5. Does NOT write the geo cache.

**Raises**:
- `DuplicatePlaceError` — if any row violates the partial unique index on `provider_id`. The transaction is rolled back; nothing is inserted. The error's `.conflicts` list contains one `DuplicateProviderId` per conflicting row, in the order they appeared in `items`.
- `RuntimeError` — on any other database failure.

**Atomicity**:
- All-or-nothing per FR-006a. There is no partial-success path. Callers wanting partial-success semantics must split the batch themselves and call `create()` per row, or pre-filter via `get_by_external_id`.

**Order**:
- Output preserves input order exactly. Length of output equals length of input on success.

---

### `get(place_id: str) -> PlaceObject | None`

Fetch one place from the permanent store by internal ID.

**Inputs**:
- `place_id: str` — the internal UUID-string `id`.

**Behavior**:
1. Calls `repo.get(place_id)`.
2. Returns the `PlaceObject` with Tier 1 fields populated and `geo_fresh=False`, `enriched=False`. Tier 2/3 fields are `None`.
3. Does NOT touch Redis. Reading the caches is the caller's job via `enrich_batch` if it wants them.
4. Returns `None` if no row with that `place_id` exists.

**Raises**:
- `RuntimeError` — on database failure.

**Authorization**: not enforced. Per spec FR-007 (clarification Q1), the data layer trusts the caller. The route or agent tool layer is responsible for verifying the requesting user owns this `place_id` before calling.

---

### `get_batch(place_ids: list[str]) -> list[PlaceObject]`

Fetch many places by internal ID.

**Inputs**:
- `place_ids: list[str]`.

**Behavior**:
1. If `place_ids == []`: returns `[]` immediately, no DB call.
2. Calls `repo.get_batch(place_ids)`.
3. Returns Tier 1 only `PlaceObject`s, `geo_fresh=False`, `enriched=False`.
4. Order matches input order for the rows that exist. **If a `place_id` does not exist, that position is omitted from the output** — there is NO `None` placeholder. The output length may therefore be less than the input length. This matches the existing `recall_repository.get_batch` behavior in this repo and avoids forcing every caller to handle a `None`-vs-`PlaceObject` discriminator.

> ⚠️ **CRITICAL CALLER WARNING — silent drop, not a `None` placeholder.**
> Because `get_batch` does not return `None` for missing rows, any caller that needs **positional alignment** between the input list and the output list (for example: a ranking step that pre-computed parallel arrays of scores keyed by input position; a consult node that joins `places[i]` against `candidates[i]`; any `zip(input_ids, get_batch(input_ids))` pattern) will **silently misalign** when even one ID is missing.
>
> Such callers MUST use `get(place_id)` per ID instead, and explicitly handle `None` themselves. The wiring tasks in plan.md § Code Migration Manifest call this out for `consult/`, `ranking/`, and any other site that touches a list of `place_id`s.

**Raises**: `RuntimeError` on DB failure.

---

### `enrich_batch(places: list[PlaceObject], geo_only: bool = False) -> list[PlaceObject]`

Attach Tier 2 (and optionally Tier 3) data to a list of places.

**Inputs**:
- `places: list[PlaceObject]` — typically the output of `get_batch` or another caller-built list.
- `geo_only: bool` — `True` for the recall use case (Tier 2 only, no provider call), `False` for the consult use case (both tiers, fetch on miss).

**Behavior — both modes**:
1. Empty input → returns `[]` immediately.
2. Walks `places` and collects the set of unique non-null `provider_id`s. Places with `provider_id=None` are passed through unchanged at their original positions with `geo_fresh=False`, `enriched=False`. (FR-031.)
3. Output preserves input order. (FR-032, SC-008.)

**Behavior — `geo_only=True` (recall mode)**:
4. One `cache.get_geo_batch(unique_provider_ids)` call. (One MGET round trip; FR-018, SC-004.)
5. **No** `cache.get_enrichment_batch` call. **No** `client.get_place_details` call. (FR-028.)
6. For each input place whose `provider_id` is a hit, return a copy with `lat`, `lng`, `address` populated and `geo_fresh=True`.
7. For each input place whose `provider_id` is a miss, return a copy with Tier 2 fields `None` and `geo_fresh=False`.
8. `enriched` is always `False` in this mode, even if a hot Tier 3 entry happens to exist. (FR-028 — recall never reads Tier 3.)
9. **Cache read failure**: if `cache.get_geo_batch` raises a known cache-backend error (`RedisError`, `ConnectionError`, `asyncio.TimeoutError`), the error is logged and treated as "all keys missed". Every place is returned with `geo_fresh=False`. The call does **not** raise. (FR-026a, clarification Q2.)

**Behavior — `geo_only=False` (consult mode)**:
4. One `cache.get_geo_batch(unique_provider_ids)` call.
5. One `cache.get_enrichment_batch(unique_provider_ids)` call.
6. Compute the union of cache misses across both tiers: `misses = (unique_provider_ids - geo_hits.keys()) | (unique_provider_ids - enr_hits.keys())`.
7. If `len(misses) > config.places.max_enrichment_batch`: slice `misses` to the first `max_enrichment_batch` entries (deterministic ordering — sorted by string, so tests can pin behavior), log `places.enrichment.fetch_cap_exceeded` warning with `requested`, `cap`, `dropped` keys, and continue with the truncated set. (FR-030.)
8. For each `pid` in `misses`, strip the namespace prefix via `pid.split(":", 1)[1]` and call `client.get_place_details(external_id)`. All calls are issued via a single `asyncio.gather(..., return_exceptions=True)`. (SC-003.)
9. For each successful response, build `(GeoData, PlaceEnrichment)` via `_map_provider_response` (data-model.md §5). For each failed response (Exception in the gather result), log `places.enrichment.fetch_failed` with the `provider_id` and skip — that place will pass through with whatever Tier 2/3 data was already cached, or none if neither was cached.
10. Write back the freshly fetched data:
    - `cache.set_geo_batch({pid: geo for pid, geo in new_geo.items() if geo is not None})` — one pipelined SET batch.
    - `cache.set_enrichment_batch({pid: enr for pid, enr in new_enr.items() if enr is not None})` — one pipelined SET batch.
    - Both write-back calls happen after the **single** `client.get_place_details` call per unique miss. One API call → split locally via `_map_provider_response` → two cache writes. No second API call.
    - **Cache write failure**: errors are logged and swallowed inside the cache modules. The call still returns successfully with the freshly fetched data attached. (FR-026b.)
11. Merge data onto every input place by `provider_id`:
    - Tier 2 fields populated → `geo_fresh=True`.
    - Tier 3 fields populated → `enriched=True`.
    - Both flags are independent (data-model.md §6).
12. Return the merged list in the original input order.
13. **Cache read failure on either tier**: that tier degrades to "all miss" and the affected places flow into the provider fetch path (subject to the cap). The call does **not** raise.

**Returns**: `list[PlaceObject]` of the same length and order as the input.

**Raises**:
- Does NOT raise on individual provider call failures (logged and skipped).
- Does NOT raise on cache backend failures.
- Raises `RuntimeError` only on truly unexpected exceptions (programmer errors). The cache failure handling explicitly catches `RedisError`, `ConnectionError`, `asyncio.TimeoutError` — anything else propagates so we don't swallow bugs.

---

## Helper invariants (verified by tests)

These are not part of the public surface but are observable through tests:

- **One round trip per cache tier per call**: `cache.get_geo_batch` is called at most once per `enrich_batch` invocation. Same for `cache.get_enrichment_batch`. (SC-004.)
- **One INSERT per `create_batch`**: `repo.create_batch` issues exactly one `session.execute(insert(...).returning(...))` regardless of batch size > 0. (Observed via SQLAlchemy event listener in tests, or via mock call count.)
- **Namespace string built in exactly one place**: `_build_provider_id` is the only function that constructs `"{provider}:{external_id}"`. Static check via grep in CI is acceptable but not required for this feature.
- **Namespace string parsed in exactly one place**: `_strip_namespace` (or its inline equivalent) is the only place that splits on `:`. Same.

---

## Logging keys (structured)

| Key | When emitted | Extra fields |
|---|---|---|
| `places.create.duplicate` | `repo.create` raises `DuplicatePlaceError` | `provider_id`, `existing_place_id` |
| `places.create_batch.duplicate` | `repo.create_batch` raises `DuplicatePlaceError` | `provider_ids` (list), `count` |
| `places.enrichment.cache_read_failed` | Either cache `get_batch` raised | `tier` (`"geo"` / `"enrichment"`), `provider_id_count`, `error` |
| `places.enrichment.cache_write_failed` | Either cache `set_batch` raised | `tier`, `key_count`, `error` |
| `places.enrichment.fetch_cap_exceeded` | Misses > `max_enrichment_batch` | `requested`, `cap`, `dropped` |
| `places.enrichment.fetch_failed` | A single `client.get_place_details` raised | `provider_id`, `error` |

All emit at `WARNING` (or `ERROR` for failures the caller cannot recover from). The repo's existing JSON log formatter picks up the `extra` dict.

---

## Threading / concurrency model

- All methods are `async def` and rely on the existing async session and async Redis client.
- `enrich_batch` is the only method that issues parallel work (`asyncio.gather`). Concurrency is bounded by the `max_enrichment_batch` cap.
- The service holds no mutable state. Multiple concurrent calls into the same `PlacesService` instance are safe.
- The session is per-request (FastAPI `Depends(get_session)`); the Redis client is a singleton (per ADR-019).

---

## Backwards compatibility

- The legacy `SQLAlchemyPlaceRepository` remains untouched and continues to serve `ExtractionService`. There is no shared state between it and `PlacesRepository` beyond the `places` table itself.
- The new contract does not break any existing import path. `from kebi.core.places import PlacesService, ...` is brand new; `from kebi.core.places import PlacesClient, GooglePlacesClient` (existing per ADR-049) is unchanged.
- No public Pydantic schema is renamed or repurposed in this feature. `PlacesMatchResult` and `PlacesMatchQuality` from `places_client.py` are untouched.
