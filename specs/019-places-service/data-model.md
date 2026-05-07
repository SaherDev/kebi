# Phase 1 — Data Model

**Feature**: 019-places-service
**Date**: 2026-04-14
**Companion docs**: [plan.md](./plan.md), [research.md](./research.md), [contracts/places-service.md](./contracts/places-service.md)

This document is the authoritative source of truth for: every Pydantic model the data layer exposes, every column in the new `places` table, every Redis key shape, every index, and the relocation matrix the seed migration uses to move legacy data.

---

## 1. Pydantic models (`src/kebi/core/places/models.py`)

All models are Pydantic v2 `BaseModel` unless noted. `mypy --strict` must pass.

### 1.1 Enums

```python
class PlaceType(str, Enum):
    food_and_drink = "food_and_drink"
    things_to_do  = "things_to_do"
    shopping      = "shopping"
    services      = "services"
    accommodation = "accommodation"
```

```python
class PlaceSource(str, Enum):
    tiktok    = "tiktok"
    instagram = "instagram"
    youtube   = "youtube"
    manual    = "manual"
    link      = "link"
```

```python
class PlaceProvider(str, Enum):
    google     = "google"
    foursquare = "foursquare"
    manual     = "manual"
```

**Subcategory vocabulary** (validated as plain strings; not enums, but the contract is closed and lives in this doc):

| `place_type`     | Allowed subcategory values                                                                |
| ---------------- | ----------------------------------------------------------------------------------------- |
| `food_and_drink` | `restaurant`, `cafe`, `bar`, `bakery`, `food_truck`, `brewery`, `dessert_shop`            |
| `things_to_do`   | `nature`, `cultural_site`, `museum`, `nightlife`, `experience`, `wellness`, `event_venue` |
| `shopping`       | `market`, `boutique`, `mall`, `bookstore`, `specialty_store`                              |
| `services`       | `coworking`, `laundry`, `pharmacy`, `atm`, `car_rental`, `barbershop`                     |
| `accommodation`  | `hotel`, `hostel`, `rental`, `unique_stay`                                                |

**Tag vocabulary** (open list, but the standard set is): `date-night`, `hidden-gem`, `queue-worthy`, `outdoor-seating`, `solo-friendly`, `group-friendly`, `wheelchair-accessible`, `cash-only`, `reservation-needed`, `rooftop`. Stored as `JSONB` array; no DB-level enum.

### 1.2 Structured attributes

```python
class LocationContext(BaseModel):
    neighborhood: str | None = None
    city:         str | None = None
    country:      str | None = None
```

```python
class PlaceAttributes(BaseModel):
    cuisine:          str | None = None
    # closed vocab: japanese, thai, italian, korean, chinese, mexican, indian,
    # vietnamese, french, middle_eastern, mediterranean, american, fusion

    price_hint:       str | None = None
    # closed vocab: cheap, moderate, expensive, luxury

    ambiance:         str | None = None
    # closed vocab: casual, cozy, romantic, lively, upscale, minimalist,
    # noisy, quiet, trendy, traditional

    dietary:          list[str] = []
    # closed vocab: vegetarian, vegan, halal, kosher, gluten-free, no-pork, nut-free

    good_for:         list[str] = []
    # closed vocab: date-night, solo, groups, families, business, sunset,
    # quick-bite, late-night, brunch, special-occasion

    location_context: LocationContext | None = None
    # NER-extracted from source content, not from Google addressComponents

    model_config = ConfigDict(extra="forbid")
```

`extra="forbid"` is intentional: malformed attribute payloads from a future caller should fail loudly at the boundary, not silently land in the JSONB.

### 1.3 Hours dict (TypedDict, not a Pydantic model)

```python
class HoursDict(TypedDict, total=False):
    sunday:    str | None
    monday:    str | None
    tuesday:   str | None
    wednesday: str | None
    thursday:  str | None
    friday:    str | None
    saturday:  str | None
    timezone:  str  # IANA e.g. "Asia/Tokyo" — required when any day key is present
```

Semantics:

- A day key with value `None` → closed that day.
- A missing day key → unknown for that day.
- `timezone` MUST be present whenever any day key is present. The cache module enforces this on `set_batch` (raises `ValueError` if violated; this is a programmer error, not a runtime data condition).

### 1.4 Cache value models

```python
class GeoData(BaseModel):
    lat:       float
    lng:       float
    address:   str
    cached_at: datetime

    model_config = ConfigDict(extra="forbid")
```

```python
class PlaceEnrichment(BaseModel):
    hours:      HoursDict | None = None
    rating:     float | None = None
    phone:      str | None = None
    photo_url:  str | None = None
    popularity: float | None = None  # normalized 0-1
    fetched_at: datetime

    model_config = ConfigDict(extra="forbid")
```

Both are stored in Redis via `model_dump_json()` and read via `model_validate_json()`. JSON-only — no pickle. The `datetime` fields serialize as ISO 8601 strings; Pydantic 2 handles the round trip automatically.

### 1.5 Public unified return type

```python
class PlaceObject(BaseModel):
    # Tier 1 — PostgreSQL, always present
    place_id:    str
    place_name:  str
    place_type:  PlaceType
    subcategory: str | None = None
    tags:        list[str] = []
    attributes:  PlaceAttributes = Field(default_factory=PlaceAttributes)
    source_url:  str | None = None
    source:      PlaceSource | None = None
    provider_id: str | None = None  # namespaced; built only by PlacesRepository

    # Tier 2 — Redis geo cache
    lat:        float | None = None
    lng:        float | None = None
    address:    str | None = None
    geo_fresh:  bool = False        # True only when this call hit the geo cache (or wrote it back fresh)

    # Tier 3 — Redis enrichment cache
    hours:      HoursDict | None = None
    rating:     float | None = None
    phone:      str | None = None
    photo_url:  str | None = None
    popularity: float | None = None
    enriched:   bool = False        # True only when this call populated tier-3 fields. Recall mode never sets this True.

    model_config = ConfigDict(extra="forbid")
```

**Field interaction rules** (enforced in tests, not in `__init__`):

- A new place from `create()` has `geo_fresh=False`, `enriched=False`, and all Tier 2/3 fields are `None`.
- A place from `get()` / `get_batch()` has `geo_fresh=False`, `enriched=False`, and all Tier 2/3 fields are `None`.
- A place from `enrich_batch(geo_only=True)` may have `geo_fresh=True` with Tier 2 fields populated, but `enriched` is always `False` and Tier 3 fields are always `None`, even if a hot enrichment entry happens to exist (per spec FR-028, recall mode never reads Tier 3).
- A place from `enrich_batch(geo_only=False)` has `geo_fresh=True` and `enriched=True` for any place whose `provider_id` was non-null _and_ the cache+fetch flow completed (hit or successful fetch). Places whose `provider_id` is `None` pass through with both flags `False`.

### 1.6 Write input

```python
class PlaceCreate(BaseModel):
    user_id:     str
    place_name:  str
    place_type:  PlaceType
    subcategory: str | None = None
    tags:        list[str] = []
    attributes:  PlaceAttributes = Field(default_factory=PlaceAttributes)
    source_url:  str | None = None
    source:      PlaceSource | None = None
    external_id: str | None = None      # raw provider ID, no namespace prefix
    provider:    PlaceProvider | None = None

    model_config = ConfigDict(extra="forbid")
```

**Validation rules**:

- Exactly zero or both of `external_id` and `provider` may be present. If only one is set, the model raises `ValueError` at construction time. Enforced via a `model_validator(mode="after")`.
- `place_name` is non-empty (Pydantic `min_length=1`).
- `user_id` is non-empty.
- `subcategory`, when present, must belong to the vocabulary for `place_type`. Enforced via the same `model_validator`.

### 1.7 Error type

```python
@dataclass(frozen=True, slots=True)
class DuplicateProviderId:
    provider_id: str            # the namespaced string
    existing_place_id: str      # the internal place_id of the row that already exists

class DuplicatePlaceError(Exception):
    """Raised by PlacesRepository.create / create_batch on provider_id collision."""

    def __init__(self, conflicts: list[DuplicateProviderId]) -> None:
        self.conflicts = conflicts
        super().__init__(
            f"Duplicate provider_id(s): {', '.join(c.provider_id for c in conflicts)}"
        )
```

Used by both `create()` (always one element in `conflicts`) and `create_batch()` (one or more elements). Callers interrogate `.conflicts` to recover; they do not pattern-match the message string.

---

## 2. PostgreSQL schema — new shape after this feature

> **Updated 2026-04-14 (user override)**: Single Alembic revision. Feature 019 adds the new columns, runs the seed migration, and **drops the legacy columns** in the same migration. There is no second revision and no follow-up feature for the schema. The phrase "revision A / revision B" elsewhere in this document is historical — read it as "this single revision" and "the deferred drops happen in this same revision". Every reader/writer of the legacy fields is migrated in the same PR (see plan.md § Code Migration Manifest).

### 2.1 Final-state column list (target shape, after this single revision)

| Column        | Type                | Nullable | Notes                                                                                                                                                    |
| ------------- | ------------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`          | `String` (PK)       | NO       | UUID string, generated by `PlacesRepository` (uuid4)                                                                                                     |
| `user_id`     | `String`, indexed   | NO       | Stamped from `PlaceCreate.user_id`. Not enforced as FK (cross-repo boundary, constitution VI).                                                           |
| `created_at`  | `DateTime(tz=True)` | NO       | `server_default=now()`                                                                                                                                   |
| `updated_at`  | `DateTime(tz=True)` | NO       | `server_default=now()`, `onupdate=now()`                                                                                                                 |
| `place_name`  | `String`, length≥1  | NO       |                                                                                                                                                          |
| `place_type`  | `String`            | NO       | One of the `PlaceType` enum values. Stored as plain string (no DB enum) to avoid migration friction when adding new types.                               |
| `subcategory` | `String`            | YES      | One of the subcategory vocabulary values for `place_type`; validated at the Pydantic boundary.                                                           |
| `tags`        | `JSONB`             | YES      | List of strings. `NULL` is normalized to `[]` on read.                                                                                                   |
| `attributes`  | `JSONB`             | YES      | Serialized `PlaceAttributes`. `NULL` is normalized to default `PlaceAttributes()` on read.                                                               |
| `source_url`  | `Text`              | YES      |                                                                                                                                                          |
| `source`      | `String`            | YES      | One of the `PlaceSource` enum values. (This column already exists in the legacy schema as `String`; the value vocabulary tightens but the column stays.) |
| `provider_id` | `String`            | YES      | Namespaced: `"{provider}:{external_id}"`. `NULL` allowed for places without provider attribution.                                                        |

**Constraints**:

- Primary key on `id`.
- Partial unique index: `CREATE UNIQUE INDEX uq_places_provider_id ON places(provider_id) WHERE provider_id IS NOT NULL;`
- B-tree index on `user_id`.
- Composite B-tree index on `(user_id, place_type)`.
- GIN FTS index: `CREATE INDEX places_fts_idx ON places USING gin(to_tsvector('english', coalesce(place_name, '') || ' ' || coalesce(subcategory, '')));`

### 2.2 Legacy columns dropped in this revision

After the seed migration script runs, the following columns are dropped in the same Alembic revision via `op.drop_column`:

| Legacy column       | Where its data goes (via seed migration)                                                                                                 |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `address`           | Redis `places:geo:{provider_id}` for rows with `provider_id`; lost (logged) for rows without                                             |
| `cuisine`           | `attributes.cuisine` JSONB                                                                                                               |
| `price_range`       | `attributes.price_hint` JSONB (mapped: `low→cheap`, `mid→moderate`, `high→expensive`)                                                    |
| `lat`               | Redis `places:geo:{provider_id}` for rows with `provider_id`; lost (logged) otherwise                                                    |
| `lng`               | Redis `places:geo:{provider_id}` for rows with `provider_id`; lost (logged) otherwise                                                    |
| `external_provider` | Folded into `provider_id` as the namespace prefix                                                                                        |
| `external_id`       | Folded into `provider_id` as the suffix                                                                                                  |
| `confidence`        | Discarded — extraction confidence is a process metric, not a place attribute. Re-derived per extraction by ExtractionService internally. |
| `validated_at`      | Discarded — was a Google-validation timestamp; the Tier 2 cache's `cached_at` replaces it                                                |
| `ambiance`          | `attributes.ambiance` JSONB                                                                                                              |

The legacy `uq_places_provider_external` composite unique constraint is dropped at the same time (replaced by `uq_places_provider_id` partial unique index).

(The current schema does not have `photo_url`, `hours`, `rating`, `phone`, `popularity` columns despite the brief listing them as "drop". They never existed. The brief was being defensive. The migration confirms their non-existence and skips them.)

### 2.3 Backfill of `provider_id` (runs in this revision, before the partial unique index is added)

```sql
UPDATE places
SET provider_id = external_provider || ':' || external_id
WHERE external_provider IS NOT NULL
  AND external_id        IS NOT NULL
  AND provider_id        IS NULL;
```

Then the partial unique index is created. Any rows that lack `external_provider` or `external_id` keep `provider_id` NULL and are not subject to the unique constraint. (Per the existing schema, every row has `external_provider` set — usually `"google"` or `"unknown"` — but `external_id` is nullable. Rows where `external_id` is `NULL` keep `provider_id` `NULL` as expected.)

### 2.4 Indexes — full DDL

```sql
-- Partial unique index for provider_id (allows many NULLs, at most one per non-null value)
CREATE UNIQUE INDEX uq_places_provider_id
  ON places(provider_id)
  WHERE provider_id IS NOT NULL;

-- User filter (already exists from the legacy schema — kept)
CREATE INDEX IF NOT EXISTS ix_places_user_id ON places(user_id);

-- User + place_type composite (new)
CREATE INDEX ix_places_user_type ON places(user_id, place_type);

-- FTS on name + subcategory (new)
CREATE INDEX places_fts_idx ON places
  USING gin(to_tsvector('english', coalesce(place_name, '') || ' ' || coalesce(subcategory, '')));
```

The legacy `uq_places_provider_external` composite unique constraint is dropped in this same revision, immediately after the partial unique on `provider_id` is created and validated.

### 2.5 SQLAlchemy model deltas (`src/kebi/db/models.py`)

The `Place` ORM class is reshaped to its final form in this revision:

```python
class Place(Base):
    __tablename__ = "places"

    id:          Mapped[str]            = mapped_column(String, primary_key=True)
    user_id:     Mapped[str]            = mapped_column(String, nullable=False, index=True)
    created_at:  Mapped[datetime]       = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at:  Mapped[datetime]       = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    place_name:  Mapped[str]            = mapped_column(String, nullable=False)
    place_type:  Mapped[str]            = mapped_column(String, nullable=False)
    subcategory: Mapped[str | None]     = mapped_column(String, nullable=True)
    tags:        Mapped[list[str] | None]      = mapped_column(JSONB, nullable=True)
    attributes:  Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    source_url:  Mapped[str | None]     = mapped_column(Text, nullable=True)
    source:      Mapped[str | None]     = mapped_column(String, nullable=True)
    provider_id: Mapped[str | None]     = mapped_column(String, nullable=True)

    embeddings:  Mapped[list["Embedding"]] = relationship(
        "Embedding", back_populates="place", cascade="all, delete-orphan"
    )
```

`place_type` is `NOT NULL` from the start. The seed migration backfills it for every legacy row before the column is constrained.

> ⚠️ **Operator review gate**: rows that cannot be classified are defaulted to `place_type='services'` with `subcategory=NULL` and a `place_type_defaulted` log line. **`'services'` rows in the post-migration database may be wrong** — the heuristic ladder is intentionally cheap, not accurate. The seed script exits with non-zero status when any row was defaulted, so the operator cannot accidentally promote the migration without seeing the warning. The operator must (a) review `scripts/seed_migration.log` for every defaulted row, (b) decide whether to manually re-classify them (e.g. via SQL UPDATE), re-run extraction on their `source_url`s to let the LLM re-derive `place_type`, or accept the default — and (c) re-run the seed script with `--accept-defaults` to clear the gate. Only then does `alembic upgrade head` proceed. The Alembic migration file's header comment carries this instruction.

`PlacesRepository` is the **only** code that reads or writes `Place` ORM rows. Outside the repository, every other service in the app sees `PlaceObject`. The legacy fields no longer exist on the ORM, so accidental references will fail at type-check time (mypy --strict).

---

## 3. Redis — Tier 2 and Tier 3 cache shapes

Both tiers live in a single `PlacesCache` class (`src/kebi/core/places/cache.py`) that holds the Redis client reference and exposes four methods: `get_geo_batch`, `set_geo_batch`, `get_enrichment_batch`, `set_enrichment_batch`. Both tiers share the same TTL (`config.places.cache_ttl_days * 86400` seconds, default 30 days).

### 3.1 Tier 2: geo cache

- **Key**: `places:geo:{provider_id}` where `{provider_id}` is the full namespaced string (e.g. `places:geo:google:ChIJN1t_tDeuEmsRUsoyG83frY4`).
- **Value**: `GeoData.model_dump_json()` UTF-8 bytes.
- **TTL**: `config.places.cache_ttl_days * 86400` seconds. Default 30 days = 2_592_000 seconds.
- **Read**: `redis.mget([key1, key2, ...])` — single round trip. Missing keys come back as `None`.
- **Write**: pipelined `SET key value EX ttl` for each item, single round trip via `pipeline(transaction=False)`.

### 3.2 Tier 3: enrichment cache

- **Key**: `places:enrichment:{provider_id}`.
- **Value**: `PlaceEnrichment.model_dump_json()` UTF-8 bytes.
- **TTL**: `config.places.cache_ttl_days * 86400` seconds (same TTL as Tier 2).
- **Read/Write**: same MGET / pipelined SET pattern as Tier 2.

### 3.3 Key collision and isolation

The `places:` prefix is reserved for this feature. No other module in the repo uses `places:geo:*` or `places:enrichment:*`. The existing LLM cache uses `cache:llm:*` (per ADR-024); the existing extraction status cache uses `extract:status:*` (per ADR-048). No overlap.

### 3.4 What is NOT cached

- The permanent-store row. Reads always go to PostgreSQL. There is no place-row cache.
- The `PlaceCreate` input. The repository writes synchronously and returns.
- The provider client's HTTP responses. The provider client may have its own cache, but that is independent of Tier 2/Tier 3.

---

## 4. Seed migration: relocation matrix (`scripts/seed_migration.py`)

**Run order**: `python scripts/seed_migration.py` BEFORE `alembic upgrade head` for revision A. The Alembic file's header comment instructs the operator.

**What it does** (per row in the legacy `places` table):

| Legacy field present                                                       | Destination                                                                       | Behavior                                                                                                                                                                                                                                                                                                                                                                                                    |
| -------------------------------------------------------------------------- | --------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cuisine` not null                                                         | `attributes.cuisine` (JSONB)                                                      | Read existing `attributes` JSONB if any; merge `{"cuisine": <value>}` into it; write back.                                                                                                                                                                                                                                                                                                                  |
| `price_range` in `{"low","mid","high"}`                                    | `attributes.price_hint`                                                           | Map: `low → cheap`, `mid → moderate`, `high → expensive`. Anything else (NULL or unrecognized) is left unmapped and logged as `unmapped_price_range`.                                                                                                                                                                                                                                                       |
| `ambiance` not null                                                        | `attributes.ambiance`                                                             | Direct copy.                                                                                                                                                                                                                                                                                                                                                                                                |
| `lat`, `lng`, `address` all not null AND `provider_id` not null            | Redis `places:geo:{provider_id}`                                                  | Build `GeoData(lat, lng, address, cached_at=now())` and `SET ... EX <geo_ttl>`. Use a single Redis pipeline for the whole batch of seedable rows.                                                                                                                                                                                                                                                           |
| `lat`/`lng`/`address` present but `provider_id` is null                    | DROPPED — logged as `geo_data_lost_no_provider_id` with the row id and place_name | These rows had no way to be re-fetched anyway. Logged for visibility.                                                                                                                                                                                                                                                                                                                                       |
| `place_type` not yet set (every legacy row, since the column is brand new) | `place_type` column (NOT NULL)                                                    | Inferred from existing data using a simple heuristic ladder: (1) if `cuisine` is non-null → `food_and_drink`; (2) if `external_provider == 'google'` and `place_name` matches a known nature/museum/site keyword set → `things_to_do`; (3) if no signal → defaulted to `services` with a `place_type_defaulted` warning logged for that row id. After backfill, the script ALTERs `place_type` to NOT NULL. |
| `subcategory` not yet set                                                  | `subcategory` column (nullable)                                                   | **Always left NULL for legacy rows.** A blanket "if `cuisine` is set then `subcategory='restaurant'`" mapping is too broad — `cuisine=japanese` could be a cafe, bar, or izakaya, not necessarily a restaurant. The NER pipeline (LLM enricher) sets `subcategory` correctly on the next extraction; until then NULL is the safer sentinel. The seed script does NOT touch `subcategory`.                   |

**What the script does NOT do**:

- It does not delete legacy columns. The Alembic migration (which runs after the script) does the column drops.
- It does not touch rows whose `cuisine`/`price_range`/`ambiance`/`lat`/`lng`/`address` are all NULL — those rows still receive `place_type` backfill but no attribute relocation.
- It does not call any external service. Re-classification of defaulted rows happens manually after the migration.

**Idempotency**: re-running the script must not corrupt data. It checks `attributes.cuisine` etc. before writing; if a value already exists, it logs `attributes_cuisine_already_set` and skips that field. Redis SETs are unconditionally re-issued (TTL refresh is a feature, not a bug).

**Output report** (printed to stdout and to `scripts/seed_migration.log`):

```
seed_migration: scanned N rows
  cuisine relocated:        X
  price_range mapped:       Y  (unmapped: Z)
  ambiance relocated:       W
  geo cache seeded:         G
  geo data lost (no pid):   L  (logged with row ids)
  place_type inferred:      P  (food_and_drink: F, things_to_do: T, defaulted: D)
done.

⚠️  D rows were defaulted to place_type='services'. Review scripts/seed_migration.log
    for "place_type_defaulted" lines BEFORE promoting this migration to production.
    Each defaulted row has its id and place_name logged. Re-run extraction on those
    rows' source_url after deployment to derive a better place_type.
```

**Operator gate**: the script's exit code is non-zero (specifically `2`) whenever `D > 0`. This forces the operator to acknowledge the defaulted rows. To proceed without re-classification, re-run with `--accept-defaults`, which sets exit code `0` and writes an `accepted_defaults` line to the log. The Alembic migration's pre-flight check (the comment in the migration file's header) instructs the operator: "if seed_migration.py exits non-zero, review the log and either fix the heuristic ladder, manually update the rows, or re-run with --accept-defaults BEFORE running alembic upgrade head."

---

## 5. Mapping from `PlacesClient.get_place_details()` response → cache shapes

The brief specifies that `get_place_details(external_id)` returns a dict with keys: `lat`, `lng`, `address`, `hours` (with `timezone`), `rating`, `phone`, `photo_url`, `popularity`. `PlacesService.enrich_batch` is responsible for splitting that dict into a `GeoData` and a `PlaceEnrichment`.

Mapping (executed inside a private helper `_map_provider_response` in `service.py`):

```python
def _map_provider_response(
    response: dict[str, Any],
) -> tuple[GeoData | None, PlaceEnrichment | None]:
    now = datetime.now(timezone.utc)

    geo: GeoData | None = None
    if response.get("lat") is not None and response.get("lng") is not None and response.get("address"):
        geo = GeoData(
            lat=float(response["lat"]),
            lng=float(response["lng"]),
            address=str(response["address"]),
            cached_at=now,
        )

    enr = PlaceEnrichment(
        hours=response.get("hours"),
        rating=response.get("rating"),
        phone=response.get("phone"),
        photo_url=response.get("photo_url"),
        popularity=response.get("popularity"),
        fetched_at=now,
    )
    return geo, enr
```

Both halves may be present, only one, or neither (e.g. provider returned a near-empty response). The service writes whichever halves are non-None to their respective caches.

`get_place_details` does not yet exist on the existing `GooglePlacesClient` (the current methods are `validate_place`, `discover`, `validate`, `geocode`). Adding it is in scope for this feature — it lives next to the existing methods in `core/places/places_client.py`. The implementation calls Google Places **Place Details** API with the fields `geometry,formatted_address,opening_hours,rating,formatted_phone_number,photos,user_ratings_total` and maps to the dict above. (Implementation detail; the `PlacesService` itself only sees the dict.)

---

## 6. State machine — what `PlaceObject.geo_fresh` and `enriched` mean across the lifecycle

There is no place-level state machine — the row is essentially write-once for permanent fields and the cache tiers are TTL-bound. But the _call-level_ freshness state is meaningful:

| Call                                                       | `geo_fresh` | `enriched` | Tier 2 fields            | Tier 3 fields            |
| ---------------------------------------------------------- | ----------- | ---------- | ------------------------ | ------------------------ |
| `create()`, `create_batch()`                               | `False`     | `False`    | `None`                   | `None`                   |
| `get()`, `get_batch()`                                     | `False`     | `False`    | `None`                   | `None`                   |
| `enrich_batch(geo_only=True)` — Tier 2 hit                 | `True`      | `False`    | populated                | `None`                   |
| `enrich_batch(geo_only=True)` — Tier 2 miss                | `False`     | `False`    | `None`                   | `None`                   |
| `enrich_batch(geo_only=True)` — `provider_id is None`      | `False`     | `False`    | `None`                   | `None`                   |
| `enrich_batch(geo_only=False)` — both tiers hit            | `True`      | `True`     | populated                | populated                |
| `enrich_batch(geo_only=False)` — Tier 2 miss, fetched      | `True`      | `True`     | populated (just fetched) | populated (just fetched) |
| `enrich_batch(geo_only=False)` — both miss, dropped by cap | `False`     | `False`    | `None`                   | `None`                   |
| `enrich_batch(geo_only=False)` — fetch raised              | `False`     | `False`    | `None`                   | `None`                   |
| `enrich_batch(geo_only=False)` — `provider_id is None`     | `False`     | `False`    | `None`                   | `None`                   |

**Invariant**: `enriched=True` implies `geo_fresh=True` only when both tiers were populated this call. The two flags are independent — it is _technically_ possible (though unusual) for Tier 2 to fail and Tier 3 to succeed (e.g. cache reads succeeded for enrichment but failed for geo). The flags track tier population state independently.

---

## 7. Validation rules summary (FR → enforcement location)

| FR                                                 | Where enforced                                                                                 |
| -------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| FR-001..010 (permanent store shape)                | SQLAlchemy column definitions + `PlacesRepository`                                             |
| FR-002 (namespace built only inside repo)          | `PlacesRepository._build_provider_id` is the single construction site; reviewed in code review |
| FR-005 (unique provider_id)                        | `uq_places_provider_id` partial unique index                                                   |
| FR-005a (DuplicatePlaceError)                      | `PlacesRepository.create` `try/except IntegrityError` block                                    |
| FR-006 (batch insert one statement)                | `insert(...).returning(...)` in `create_batch`                                                 |
| FR-006a (all-or-nothing)                           | Single transaction; rollback on `IntegrityError`                                               |
| FR-007 (no user_id on reads)                       | Method signatures in `service.py` and `repository.py`                                          |
| FR-007a (writes stamp user_id)                     | `PlaceCreate.user_id` → `Place.user_id` mapping in `_to_orm`                                   |
| FR-011 (place_type vocabulary)                     | `PlaceType` enum; Pydantic validation on input                                                 |
| FR-014 (attributes shape)                          | `PlaceAttributes` Pydantic model with `extra="forbid"`                                         |
| FR-016..019 (geo cache contract)                   | `PlacesCache.get_geo_batch` / `set_geo_batch`                                                  |
| FR-020..023 (enrichment cache contract)            | `PlacesCache.get_enrichment_batch` / `set_enrichment_batch`                                    |
| FR-024..026 (unified return type, freshness flags) | `PlaceObject` model + service-level field assignment                                           |
| FR-026a/b/c (cache failure modes)                  | `try/except` blocks in `service.py` and inside cache modules                                   |
| FR-027..033 (enrichment workflows)                 | `PlacesService.enrich_batch`                                                                   |
| FR-029a (within-batch dedupe)                      | Set construction at top of `enrich_batch`; cap counts unique                                   |
| FR-030 (fetch cap)                                 | Slice + warning log in `enrich_batch`                                                          |
| FR-034..036 (migration)                            | `alembic/versions/XXX_places_service_schema.py` + `scripts/seed_migration.py`                  |
| FR-037 (config-driven TTLs)                        | `PlacesConfig` Pydantic in `core/config.py`; values from `config/app.yaml`                     |

---

## 8. Out-of-scope clarifications (deferred to follow-up features)

- `subcategory` validation against the per-`place_type` vocabulary is enforced at the Pydantic boundary, not the DB. Adding a CHECK constraint is deferred.
- The `tags` and `attributes` JSONB columns have no GIN indexes in this revision. They will be added when a query path actually needs them.
- No GIST or pgvector indexes on `places` are added or modified in this feature. The `embeddings` table is untouched.
- Re-classification of any legacy row whose `place_type` was defaulted to `services` by the seed script — operator runs extraction again on that row's `source_url` to re-derive a better `place_type`. Not automated in this feature.
