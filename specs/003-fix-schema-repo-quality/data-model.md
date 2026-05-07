# Data Model: Schema, Repository, and Code Quality Fixes

**Feature**: 003-fix-schema-repo-quality
**Date**: 2026-03-25

---

## Changed Entity: Place (`places` table)

### Before

| Column | Type | Constraints |
|--------|------|-------------|
| id | VARCHAR | PK |
| user_id | VARCHAR | NOT NULL, INDEX |
| place_name | VARCHAR | NOT NULL |
| address | VARCHAR | NOT NULL |
| cuisine | VARCHAR | NULL |
| price_range | VARCHAR | NULL |
| lat | FLOAT | NULL |
| lng | FLOAT | NULL |
| source_url | TEXT | NULL |
| validated_at | TIMESTAMPTZ | NULL |
| **google_place_id** | **VARCHAR** | **NULL, INDEX** |
| confidence | FLOAT | NULL |
| source | VARCHAR | NULL |
| created_at | TIMESTAMPTZ | server_default=now() |
| updated_at | TIMESTAMPTZ | server_default=now(), onupdate=now() |

### After

| Column | Type | Constraints |
|--------|------|-------------|
| id | VARCHAR | PK |
| user_id | VARCHAR | NOT NULL, INDEX |
| place_name | VARCHAR | NOT NULL |
| address | VARCHAR | NOT NULL |
| cuisine | VARCHAR | NULL |
| price_range | VARCHAR | NULL |
| lat | FLOAT | NULL |
| lng | FLOAT | NULL |
| source_url | TEXT | NULL |
| validated_at | TIMESTAMPTZ | NULL |
| **external_provider** | **VARCHAR** | **NOT NULL** |
| **external_id** | **VARCHAR** | **NULL** |
| confidence | FLOAT | NULL |
| source | VARCHAR | NULL |
| created_at | TIMESTAMPTZ | server_default=now() |
| updated_at | TIMESTAMPTZ | server_default=now(), onupdate=now() |

### Constraints (new)

```sql
CREATE UNIQUE INDEX uq_places_provider_external
    ON places (external_provider, external_id)
    WHERE external_id IS NOT NULL;
```

The partial index (`WHERE external_id IS NOT NULL`) means rows where `external_id` is NULL are not subject to the uniqueness check. Two places without a known external ID can coexist even with the same provider.

### Mutable fields (updated on upsert)

The following fields are overwritten when an existing `(external_provider, external_id)` pair is re-submitted:
- `place_name`
- `address`
- `cuisine`
- `price_range`
- `lat`
- `lng`
- `source_url`
- `validated_at`
- `confidence`
- `source`
- `updated_at` (automatic via `onupdate`)

The following fields are **immutable** after creation:
- `id`
- `user_id`
- `external_provider`
- `external_id`
- `created_at`

---

## Changed Model: PlacesMatchResult

Located in `src/kebi/core/extraction/places_client.py`.

### Before

```python
class PlacesMatchResult(BaseModel):
    match_quality: PlacesMatchQuality
    validated_name: str | None = None
    google_place_id: str | None = None
    lat: float | None = None
    lng: float | None = None
```

### After

```python
class PlacesMatchResult(BaseModel):
    match_quality: PlacesMatchQuality
    validated_name: str | None = None
    external_provider: str = "google"   # set by the client implementation
    external_id: str | None = None      # provider's own ID for the place
    lat: float | None = None
    lng: float | None = None
```

---

## New Protocol: PlaceRepository

Located in `src/kebi/db/repositories/place_repository.py`.

```python
class PlaceRepository(Protocol):
    async def get_by_provider(
        self, provider: str, external_id: str
    ) -> Place | None: ...

    async def save(self, place: Place) -> Place: ...
```

`save()` implements upsert semantics:
- If `(place.external_provider, place.external_id)` exists → update all mutable fields, commit, return updated record
- If not → add new record, commit, return new record
- On any exception → rollback, log error with context, re-raise as `RuntimeError`

`get_by_provider()` is only called when `external_id` is not None (nullable external IDs skip dedup).

---

## Validation Rules

| Field | Rule |
|-------|------|
| `external_provider` | Required, non-empty string. Validated at API boundary (Pydantic) before any DB operation. Rejected with 400 if null or empty. |
| `external_id` | Optional. When present, combined with `external_provider` must be unique across all records. |
| `(external_provider, external_id)` | Unique pair enforced by partial DB index when `external_id IS NOT NULL`. |

---

## Migration Summary

**Revision**: `001_provider_agnostic_place_identity`

Steps (single Alembic revision):
1. `ALTER TABLE places ADD COLUMN external_provider VARCHAR NOT NULL DEFAULT 'google'`
2. `ALTER TABLE places ADD COLUMN external_id VARCHAR`
3. `UPDATE places SET external_id = google_place_id` (backfill)
4. `ALTER TABLE places ALTER COLUMN external_provider DROP DEFAULT`
5. `CREATE UNIQUE INDEX uq_places_provider_external ON places (external_provider, external_id) WHERE external_id IS NOT NULL`
6. `DROP INDEX IF EXISTS ix_places_google_place_id`
7. `ALTER TABLE places DROP COLUMN google_place_id`

Downgrade reverses steps in order (drops index, re-adds `google_place_id`, backfills from `external_id` where `external_provider='google'`).
