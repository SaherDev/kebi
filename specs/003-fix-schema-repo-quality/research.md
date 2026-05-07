# Research: Schema, Repository, and Code Quality Fixes

**Feature**: 003-fix-schema-repo-quality
**Date**: 2026-03-25
**Status**: Complete — no NEEDS CLARIFICATION remain

---

## Decision 1: SQLAlchemy async upsert strategy (H1/H2)

**Decision**: Get-then-update pattern (not `merge()`, not `INSERT ... ON CONFLICT`).

**Rationale**: `session.merge()` requires the object to be detached and re-attached — awkward with `expire_on_commit=False`. PostgreSQL `ON CONFLICT DO UPDATE` (via `insert().on_conflict_do_update()`) is efficient but generates raw SQL, bypassing ORM-level field tracking and making mypy difficult. The get-then-update pattern is explicit, fully typed, and testable with a `FakePlaceRepository`.

**Pattern**:
```python
async def save(self, place: Place) -> Place:
    try:
        existing = await self._get_by_provider(place.external_provider, place.external_id)
        if existing:
            existing.place_name = place.place_name
            # ... update all mutable fields
            await self._session.commit()
            return existing
        self._session.add(place)
        await self._session.commit()
        return place
    except Exception as e:
        await self._session.rollback()
        raise RuntimeError(
            f"Failed to save place ({place.external_provider}/{place.external_id}): {e}"
        ) from e
```

**Alternatives considered**:
- `session.merge()` — rejected: requires detached object pattern, unclear with async
- `INSERT ... ON CONFLICT DO UPDATE` — rejected: raw SQL bypasses ORM typing, harder to test

---

## Decision 2: Alembic migration for C1 (schema backfill)

**Decision**: Three-step migration in a single revision — add columns with defaults, backfill data, add constraint + drop old column. No separate revisions needed since there is no production data at risk (this is greenfield/dev stage).

**Migration steps in order**:
1. Add `external_provider VARCHAR NOT NULL DEFAULT 'google'`
2. Add `external_id VARCHAR` (nullable)
3. `UPDATE places SET external_id = google_place_id` (backfill)
4. Remove the default from `external_provider` (now that backfill is done)
5. `CREATE UNIQUE INDEX uq_places_provider_external ON places (external_provider, external_id) WHERE external_id IS NOT NULL`
6. Drop index on `google_place_id`
7. Drop column `google_place_id`

**Rationale**: Single migration is safe because (a) no production data exists yet, (b) backfill is instantaneous at current data volume, (c) the partial index on `WHERE external_id IS NOT NULL` correctly handles nullable external_id — two rows with `external_id=NULL` do not violate uniqueness (PostgreSQL NULL != NULL in unique constraints).

**Alternatives considered**:
- Zero-downtime multi-step migration — rejected: no live production traffic yet, adds unnecessary complexity
- Full table drop and recreate — rejected: not safe if any data exists (spec requires backfill)

---

## Decision 3: `PlacesMatchResult` field naming (C1 ripple)

**Decision**: Rename `google_place_id` → `external_id` in `PlacesMatchResult` and add `external_provider: str = "google"` as a field. The Google Places client always sets `external_provider="google"`.

**Rationale**: The `PlacesMatchResult` is the client's output — naming it `external_id` makes it provider-agnostic now. When a Yelp client is added, its `PlacesMatchResult` will set `external_provider="yelp"`. The service then maps these fields directly to the `Place` model without any hardcoded provider string in business logic.

**Alternatives considered**:
- Keep `google_place_id` in `PlacesMatchResult`, map in service — rejected: service would hardcode `"google"` string, violating provider-agnostic intent
- Add both `google_place_id` (old) and `external_id` (new) — rejected: duplication and confusion

---

## Decision 4: Consult endpoint OpenAPI documentation (H3)

**Decision**: Add `status_code=200` to the `@router.post` decorator. Do not set `response_model` (would break `StreamingResponse`). Use the `responses` parameter to document both response shapes in OpenAPI.

**Pattern**:
```python
@router.post(
    "/consult",
    status_code=200,
    responses={
        200: {
            "description": "Synchronous recommendation (stream=false)",
            "model": SyncConsultResponse,
        },
    },
)
```

**Rationale**: FastAPI's `response_model` enforces serialization at response time — this breaks `StreamingResponse`. The `responses` dict provides OpenAPI documentation without enforcing serialization. `status_code=200` makes the spec complete.

**Alternatives considered**:
- `response_model=SyncConsultResponse` — rejected: causes runtime error when streaming response is returned
- Leave undocumented — rejected: violates FR-006 and SC-004

---

## Decision 5: `PlaceRepository` location in src tree

**Decision**: `src/kebi/db/repositories/place_repository.py` — under `db/` because it is a database access concern, not a domain concern. The `ExtractionService` (in `core/`) depends on the Protocol only; it never imports from `db/repositories/` directly.

**Rationale**: ADR-002 hybrid layout puts database concerns under `db/`. ADR-038 says concrete implementations live in the relevant module for domain-specific ones — but the repository is DB-layer, not domain-layer. `core/extraction/service.py` depends on the `PlaceRepository` Protocol (imported from `db/repositories/`), wired by `deps.py`.

---

## Decision 6: UniqueConstraint scope (external_provider, external_id)

**Decision**: Unique constraint is on `(external_provider, external_id)` globally (not scoped by `user_id`). Partial: only enforced `WHERE external_id IS NOT NULL`.

**Rationale**: Current dedup logic does not scope by `user_id` (existing behavior preserved). Places are physical locations that are provider-globally unique. Two users saving "Nobu" from Google get two `Place` rows (different `user_id`), but the uniqueness constraint only fires if the same `(provider, id)` pair is submitted — which means the same physical place. The existing behavior returns the first-inserted record for any user on dedup hit; that behavior is preserved. Adding `user_id` to the constraint would be a separate scope change not in scope of this feature.

