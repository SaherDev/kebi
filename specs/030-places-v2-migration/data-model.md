# Phase 1 Data Model — Extraction → Places v2 Cutover

What changes shape, what stays, and the exact conversion rules. Pairs with [research.md](./research.md) R-01 (mapping table) and R-05 (`ValidatedCandidate` reshape).

---

## Entities

### Place (canonical) — **changed**

Before: `kebi.core.places.PlaceObject` (legacy module). After: `kebi.core.places_v2.PlaceObject` (extends `PlaceCore`).

Fields used by extraction (after):

| Field | Type | Source | Notes |
|---|---|---|---|
| `id` | `str \| None` | DB-assigned | Set after upsert. |
| `provider_id` | `str` (namespaced) | Google search step | `"google:<placeId>"`. Validated by `PlaceCore._validate_provider_id`. |
| `place_name` | `str` | Google → picker | Sticky on upsert (merge policy keeps the first canonical name). |
| `place_name_aliases` | `list[PlaceNameAlias]` | Picker may contribute | Deduped on upsert. |
| `categories` | `list[PlaceCategory]` | Picker | Multi-value (legacy was single). |
| `tags` | `list[PlaceTag]` | Picker | Each tag has `type`, `value`, `source="llm"`. |
| `location` | `LocationContext \| None` | Google | lat/lng/address from `PlacesSearchService` result. |
| `created_at`, `refreshed_at` | `datetime` | DB | Managed by repo. |
| Live fields (`rating`, `hours`, `phone`, `website`, `popularity`, `business_status`, `cached_at`) | various | Google → cache | Populated by `_google_mapper.map_place()`. Extraction never sets these directly. |

### Place embedding — **changed**

Before: `embeddings` table (legacy). After: `place_embeddings_v2` table (1024 dims, HNSW index, FTS column).

Written by `EmbeddingService.embed_and_store(cores: list[PlaceCore])`. Diff-then-embed: skip rows where `(hash, model_name)` already matches.

### Place cache entry — **changed**

Before: three Redis namespaces (`places:geo:`, `places:enrichment:`, `places:geocode:`). After: v2 cache (single flat namespace, written by `core/places_v2/cache.py:RedisPlacesCache`, keyed by `provider_id`).

Extraction never writes the cache directly anymore — `PlacesSearchService` writes when the cold path fires (provider fetched, results returned to caller). All extraction-side cache writes that existed in legacy `persistence.py` are removed.

### `ValidatedCandidate` — **changed** (pipeline-internal)

Before (`core/extraction/types.py`):

```
@dataclass
class ValidatedCandidate:
    place_name: str
    place_type: PlaceType
    provider: PlaceProvider
    external_id: str
    confidence: float
    evidence: list[Evidence]
    subcategory: str | None = None
    tags: list[str] = field(default_factory=list)
    attributes: PlaceAttributes = field(default_factory=PlaceAttributes)
    match_lat: float | None = None
    match_lng: float | None = None
    match_address: str | None = None
```

After:

```
@dataclass
class ValidatedCandidate:
    place_name: str
    provider_id: str                       # namespaced, e.g. "google:ChIJ..."
    categories: list[PlaceCategory]        # OR semantics, multi-value
    tags: list[PlaceTag]                   # PlaceTag(type, value, source="llm" | ...)
    confidence: float
    evidence: list[Evidence]
    subcategory: str | None = None         # free-text refinement; surfaces as a tag at the persistence boundary if desired
    location: LocationContext | None = None  # echoes Google's lat/lng/address from the search service
```

Conversion at the persistence boundary (`candidate_mapper.py`):

```
def candidate_to_core(c: ValidatedCandidate) -> PlaceCore:
    return PlaceCore(
        provider_id=c.provider_id,
        place_name=c.place_name,
        categories=c.categories,
        tags=c.tags,
        location=c.location,
    )
```

### `SearchMatch` — **deleted**

Replaced by `list[PlaceObject]` from `PlacesSearchService.find()`. The picker's input model is now the v2 `PlaceObject`.

### `ExtractionContext` — **changed (one field)**

`source: kebi.core.places.PlaceSource | None` → `source: kebi.core.places_v2.PlaceSource | None`. The `__post_init__` derivation via `url_source.source_from_url(url)` is preserved — `url_source.py` itself updates its import.

Every other field on `ExtractionContext` is unchanged (`url`, `user_id`, `supplementary_text`, `caption`, `transcript`, `platform`, `title`, `hashtags`, `location_tag`, `known_places`, `text_evidence`, `is_photo_post`, `image_urls`). The `search_matches: list[SearchMatch]` field is **removed** — the search result set is now transient (consumed inline by the picker), not stored on the context.

### `KnownPlace`, `Evidence`, `Producer`, `Medium`, `EvidenceField` — **unchanged**

Pipeline-state types. Not place identity. No legacy place imports. Stay as-is.

### Extraction response envelope — **internal change invisible externally**

`ExtractPlaceItem.place` type swaps from legacy `PlaceObject` to v2 `PlaceObject`. The externally observed JSON field set is preserved — see [contracts/http-response-parity.md](./contracts/http-response-parity.md).

### `UserPlace` (v2) — **in scope (added per ADR-071)**

Extraction writes one `user_places` row per saved place. Fields:

| Field | Type | Source | Notes |
|---|---|---|---|
| `user_place_id` | `str` (uuid4) | generated by `UserPlacesService.save_places` | Primary key. |
| `user_id` | `str` | `ExtractionContext.user_id` | From the request. |
| `place_id` | `str` | `PlaceCore.id` after `upsert_and_embed` | Foreign key into `places_v2.id`. |
| `approved` | `bool` | always `False` at save time | User flips to `True` later via the user-places API (out of scope for this feature). |
| `source` | `PlaceSource` (v2) | `ExtractionContext.source` | tiktok / instagram / youtube / google_maps_list / manual / kebi. |
| `source_url` | `str \| None` | `ExtractionContext.url` | Raw URL the user shared (None if pure-text input). |
| `saved_at` | `datetime` | now() at save time | Set by `UserPlacesService.save_places`. |

Duplicate handling: extraction pre-filters via `UserPlacesRepo.get_existing_place_ids(user_id, place_ids)` so a batch passed to `save_places` is conflict-free. Duplicates from the user's perspective surface in the response as `status="duplicate"`.

---

## State transitions

**Confidence partition is removed (ADR-071 supersedes ADR-057).** All picker outputs are saved. The user curates via `UserPlace.approved` (out-of-scope user-places API).

Response `status` values emitted by the extraction flow after the cutover:

```
ExtractPlaceItem.status ∈ {"saved", "duplicate"}
   - "saved"      → place row upserted, embedding written, user_places row created with approved=False
   - "duplicate"  → place row may or may not have been upserted (depending on whether any new contribution merged), but no new user_places row created because this user already has this place_id linked

# "needs_review" remains in the Literal type for backward compatibility but is never emitted.
```

Per-place lifecycle within an extraction request:

```
Producer enricher → known_places (name string)
                                ↓
PlacesSearchService.find(query)
   - DB hit       → cache overlay → PlaceObject
   - DB miss      → Google search → upsert (PlaceCore) → cache mset → PlaceObject
                                ↓
drop_geographic_features()     # extraction-side filter
                                ↓
LLMPlacePicker.pick(text_fields, search_results) → ValidatedCandidate(s)
                                ↓
dedup_by_provider_id()
                                ↓
PlaceUpsertService.upsert_and_embed(cores)
   - merges picker's categories/tags into the place row (existing from search step or new)
   - upserts embedding into place_embeddings_v2
   - returns persisted PlaceCores with .id populated
                                ↓
UserPlacesRepo.get_existing_place_ids(user_id, place_ids)
   - returns ids already linked to this user
                                ↓
split: to_link = persisted not in existing; duplicates = persisted in existing
                                ↓
UserPlacesService.save_places(user_id, to_link, source, source_url)
   - creates user_places rows with approved=False
                                ↓
EventDispatcher.dispatch(PlaceSaved(user_id, place_id, source))   # one per newly linked place
                                ↓
ExtractPlaceResponse envelope (v2 PlaceObject) → Redis extraction:v2:{request_id}
   - to_link  → ExtractPlaceItem(place=..., status="saved")
   - duplicates → ExtractPlaceItem(place=..., status="duplicate")
```

---

## Validation rules carried into v2

- `PlaceCore._validate_provider_id`: namespaced — must contain `":"`. Picker output validated before persistence.
- `PlaceCategory` membership: each value must be one of the enum entries; picker output validated by Instructor.
- `PlaceTag.source`: required string — extraction stamps `"llm"` for picker-derived tags, `"google"` for search-derived (already done by `_google_mapper`).
- `LocationContext`: `extra="forbid"` — extraction's emit must only set fields v2 declares.

---

## Schema migrations

**None in this feature.** v2 tables already exist (`places_v2`, `user_places`, `place_embeddings_v2`). Legacy tables stay; their teardown is the follow-up feature's job.
