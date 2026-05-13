# Contract: External HTTP Response Parity

Externally observed shapes that **must remain field-compatible** after the cutover. Spec references: **FR-007** (no external contract changes), **FR-011** (response carries v2 shape), **FR-012** (externally observed field names/types/semantics preserved), **SC-006** (byte-compatible at the field level on a fixture set).

The product repo (NestJS) is the only external caller. It deserializes these responses into TypeScript types. Any externally observable change is a breaking change in the product repo and is forbidden by this feature.

---

## Endpoints in scope

The product repo calls this repo over HTTP only. Three endpoints touch extraction's output:

| Endpoint | Method | Carries an extraction response? | Note |
|---|---|---|---|
| `/v1/chat` | POST | Yes — when the agent calls the save tool, the extraction result is embedded in the agent's response. | Unified entry per ADR-052. |
| `/v1/chat/stream` | POST (SSE) | Yes — SSE events emit reasoning steps + final extraction result. | Same payload type as `/v1/chat`. |
| `/v1/extraction/{request_id}` | GET | Yes — polled by the product repo for background extraction status. | Status envelope per ADR-048/063. |

There is *also* an internal direct route `/v1/extract` (used by tests/dev). Same payload type. Not externally called.

---

## Pydantic schema swap

`src/kebi/api/schemas/extract_place.py`:

```python
# Before
from kebi.core.places import PlaceObject

class ExtractPlaceItem(BaseModel):
    place: PlaceObject
    status: Literal["saved", "needs_review", "duplicate"]
    confidence: float

# After
from kebi.core.places_v2 import PlaceObject

class ExtractPlaceItem(BaseModel):
    place: PlaceObject     # v2 shape
    status: Literal["saved", "needs_review", "duplicate"]
    confidence: float
```

`ExtractPlaceResponse` (envelope shape from ADR-063 — `status`, `results`, `raw_input`, `request_id`, `failure_reason`, `failure_message`) is **not changed**. Only the embedded `place` swaps shape.

---

## Field-level contract (externally observed)

The JSON shape of an `ExtractPlaceItem.place` value before and after the cutover **must agree on these keys, types, and semantics**:

| External key | Type | Source (after cutover) | Notes |
|---|---|---|---|
| `place_name` | `string` | v2 `PlaceCore.place_name` | Same field name. |
| `provider_id` | `string` | v2 `PlaceCore.provider_id` | Namespaced (`"google:..."`). Already namespaced today on the legacy side per `e8b49e73d032` migration — no shape change at the API boundary. |
| `categories` | `string[]` | v2 `PlaceCore.categories` (list of `PlaceCategory` values, serialized to lowercase strings) | **New shape**: legacy emitted a single `place_type` string. Product repo TypeScript is updated to expect an array. **This is the one externally observed change.** |
| `tags` | `Tag[]` (each: `{type: string, value: string, source: string}`) | v2 `PlaceCore.tags` | Legacy emitted a free-form `attributes` object. New structured shape. **Second externally observed change.** |
| `location` | `{lat?, lng?, address?, neighborhood?, city?, country?, radius_m?}` | v2 `LocationContext` | Legacy emitted similar `location` object — fields are a superset. Optional fields preserve null-safe serialization. |
| `rating`, `hours`, `phone`, `website`, `popularity`, `business_status`, `cached_at` | various | v2 `PlaceObject` live fields | Same as legacy live fields. |
| `created_at`, `refreshed_at` | ISO 8601 datetime | v2 timestamps | Same as legacy. |

### Externally observable changes — three

1. **`place_type: string` → `categories: string[]`**
   - Old: `"place_type": "restaurant"`
   - New: `"categories": ["restaurant"]`
   - Action: product repo must update its TypeScript type before this feature ships. Coordinate via the same release.

2. **`attributes: {dietary: [...], features: [...], price: "..."}` → `tags: Tag[]`**
   - Old: `"attributes": { "dietary": ["vegan"], "features": ["outdoor_seating"], "price": "$$" }`
   - New: `"tags": [ {"type": "dietary", "value": "vegan", "source": "llm"}, {"type": "feature", "value": "outdoor_seating", "source": "llm"}, {"type": "price", "value": "$$", "source": "llm"} ]`
   - Action: same release coordination.

3. **`ExtractPlaceItem.status` no longer emits `"needs_review"`** (per ADR-071, supersedes ADR-057)
   - Old emitted set: `"saved" | "needs_review" | "duplicate"`
   - New emitted set: `"saved" | "duplicate"`
   - Literal type unchanged — `"needs_review"` stays in the schema for backward compatibility, but extraction no longer produces it. Product-repo UI code that rendered a "needs review" state can be left as defensive no-op or removed in a coordinated update; it is not load-bearing after this feature.

These three changes are documented here so they are surfaced explicitly, **not silent**. Per spec FR-012, "any divergence MUST be explicit and documented in the diff, not incidental." This document is that explicit record.

All other field names, types, and semantics are preserved.

---

## Envelope-level fields (no change)

`ExtractPlaceResponse`:

| Field | Type | Notes |
|---|---|---|
| `status` | `"pending" \| "completed" \| "failed"` | Per ADR-063, unchanged. |
| `request_id` | `string` | UUID, unchanged. |
| `raw_input` | `string` | Verbatim user input, unchanged. |
| `results` | `ExtractPlaceItem[]` | Empty iff `status != "completed"`. Per-item shape changes as above. |
| `failure_reason` | `string \| null` | Unchanged. Recently added (commit cb773e6). |
| `failure_message` | `string \| null` | Unchanged. |

---

## Polling endpoint

`GET /v1/extraction/{request_id}` returns the same `ExtractPlaceResponse` envelope. Same field invariants. Pre-cutover envelopes serialized under the previous shape will age out via the 3600s Redis TTL (ADR-063). No active invalidation needed.

---

## Test enforcement

`tests/core/extraction/test_v2_cutover_parity.py` (new, per research.md R-09):

1. **Field set parity** (SC-006): for each fixture input, capture the response JSON before the cutover commit and store under `tests/core/extraction/fixtures/v2_cutover/baselines/response_envelopes/<fixture>.json`. After the cutover, re-serialize and assert deep-key-set equality against the baseline, *modulo* the two intentional changes documented above (`place_type → categories`, `attributes → tags`).
2. **Type parity**: assert that `place.categories` is a list of strings whose values are valid `PlaceCategory` enum members; assert that `place.tags` is a list of `{type, value, source}` objects.
3. **Envelope parity**: `status`, `request_id`, `raw_input`, `failure_reason`, `failure_message` shapes unchanged.

The test runs against an in-memory FastAPI client (no Postgres / Redis network calls — services stubbed at the dependency-injection boundary).

---

## What the product repo must do (out of scope for this feature, but flagged)

Two TypeScript type updates in the product repo, deployed in the same release window:

```ts
// Before
type ExtractPlaceItem = {
  place: { place_type: string; attributes: { dietary?: string[]; features?: string[]; price?: string }; /* ... */ };
  /* ... */
};

// After
type ExtractPlaceItem = {
  place: { categories: string[]; tags: { type: string; value: string; source: string }[]; /* ... */ };
  /* ... */
};
```

The product repo update is **not** part of this feature's diff but must land in the same deploy window. Coordinate via PR description.
