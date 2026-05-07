# Research: Consult Pipeline (015)

## 1. Google Places Nearby Search API

**Decision**: Use `nearbysearch` endpoint (not `findplacefromtext`) for Step 3 discovery.

**Rationale**: `nearbysearch` accepts a lat/lng center + radius and returns all nearby places, optionally filtered by `keyword`, `type`, and `opennow`. `findplacefromtext` requires a text query and is designed for name-lookup validation — already used by `validate_place()`. The two methods serve different purposes and must not be conflated.

**Endpoint**: `https://maps.googleapis.com/maps/api/place/nearbysearch/json`

**Required params**:
- `location`: `{lat},{lng}` string
- `radius`: integer in metres (max 50,000)
- `key`: API key from secrets

**Optional filter params** (passed verbatim from `discovery_filters`):
- `keyword`: free-text keyword (e.g., "sushi")
- `type`: place type from Google's taxonomy (e.g., "restaurant")
- `opennow`: boolean string `"true"` to filter open places

**Response shape** (per candidate):
```json
{
  "place_id": "ChIJ...",
  "name": "...",
  "vicinity": "...",
  "geometry": {"location": {"lat": 0.0, "lng": 0.0}},
  "types": ["restaurant", "food"],
  "price_level": 2,
  "rating": 4.2,
  "user_ratings_total": 300,
  "opening_hours": {"open_now": true}
}
```

**Config addition needed**: add `nearbysearch_url` to `external_services.google_places` in `app.yaml`.

**Alternatives considered**:
- `textsearch` — requires a text query, not location-first; rejected.
- `findplacefromtext` — already used for name validation; using it for discovery would conflate two different use cases.

---

## 2. Haversine Distance Calculation

**Decision**: Implement `haversine_m(lat1, lng1, lat2, lng2) -> float` as a pure utility function in `src/kebi/core/utils/geo.py`. Returns distance in metres.

**Formula**:
```python
import math

def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6_371_000  # Earth radius in metres
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))
```

**Distance score formula for ranking**: `1.0 / (1.0 + distance_m / 1000.0)` — a smooth decay from 1.0 at 0m to ~0.5 at 1 km, ~0.17 at 5 km, ~0.09 at 10 km. This is monotonically decreasing and requires no radius parameter at scoring time.

**Alternatives considered**:
- Linear decay using radius: `max(0, 1 - distance_m / radius_m)` — requires passing radius into `rank()`, which the spec doesn't include.
- `geopy.distance`: external dependency; standard library `math` is sufficient.

---

## 3. PlacesClient Move (ADR-049 context)

**Decision**: Move `core/extraction/places_client.py` to `core/places/places_client.py` and update all imports. `ConsultService` imports from `core/places/` only.

**Rationale**: The `PlacesClient` Protocol now has two distinct responsibilities — extraction validation (`validate_place`) and consult discovery (`discover`, `validate`). Placing it in `core/extraction/` couples consult to the extraction module, violating the architecture boundary. A neutral `core/places/` location makes it a shared infrastructure module, not owned by either pipeline.

**Import update scope**:
- `core/extraction/validator.py` — imports `GooglePlacesClient`
- `api/deps.py` — imports `GooglePlacesClient` twice (inline in `get_event_dispatcher` and `get_extraction_pipeline`)
- Any test files referencing the old path

**Alternatives considered**:
- Keep in `core/extraction/`, re-export from `core/places/` — creates a confusing indirection and two canonical locations.
- Move to `providers/` — providers are LLM/embedding specific; Google Places is domain logic, not a provider.

---

## 4. LangGraph Deferral (ADR-050 context)

**Decision**: Implement `ConsultService` as a plain sequential async Python class for this phase. LangGraph `StateGraph` is deferred to Phase 4 when SSE streaming and parallel branches are introduced.

**Rationale**: ADR-021 mandated LangGraph for consult, but was written before the sequential-first approach was adopted. The 6-step pipeline has no branching logic, no tool selection, and no streaming — none of the LangGraph capabilities are needed now. Introducing LangGraph for sequential steps adds startup overhead (graph compilation), state schema complexity, and obscures the pipeline logic with no benefit. Phase 4 will rewrite `ConsultService` as a `StateGraph` with SSE streaming and parallel Step 2/3 branches per ADR-009.

**ADR-050 supersedes ADR-021**.

---

## 5. RecallResult lat/lng (C8 SQL update)

Both `_hybrid_vector_text_search` and `_text_only_search` in `SQLAlchemyRecallRepository` need `p.lat, p.lng` added to the `SELECT` clause. The `places` table already has `lat` and `lng` columns (written by `ExtractionService`). No migration needed.

`RecallRow` TypedDict must add `lat: float | None` and `lng: float | None`. `RecallResult` Pydantic model must add the same fields.

---

## 6. Deduplication Logic

**Decision**: After Step 3, build a `seen_place_ids: set[str]` from the saved candidates (Step 2 output). When iterating discovered candidates, skip any whose `place_id` is already in `seen_place_ids`. This preserves insert order and is O(n) in candidates.

**Rationale**: dict/set deduplication is simpler than sorting by source priority and produces a predictable result — the saved entry always wins, no merge of fields required.
