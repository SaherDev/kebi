# ConsultService — signature contract (M4)

Internal contract for `src/kebi/core/consult/service.py::ConsultService` after the M4 refactor. Consumers are (a) the flag-off `ChatService._dispatch` consult branch and (b) the M5 `consult_tool` wrapper.

## Constructor

```python
class ConsultService:
    def __init__(
        self,
        places_client: PlacesClient,
        places_service: PlacesService,
        taste_service: TasteModelService,
        recommendation_repo: RecommendationRepository | None = None,
    ) -> None: ...
```

Constructor contract:
- **Dropped** (relative to current): `intent_parser`, `recall_service`, `memory_service`. FR-006 forbids these on the main path.
- **Kept**: `places_client`, `places_service`, `taste_service`, `recommendation_repo`.
- `taste_service` is retained for the active-tier chip-filter branch only. Explicitly NOT used on the main path (no taste-profile summary load, no intent-context injection).
- `recommendation_repo` defaults to `NullRecommendationRepository()` when not supplied (same as today).

## `consult(...)` signature

```python
async def consult(
    self,
    user_id: str,
    query: str,
    saved_places: list[PlaceObject],
    filters: ConsultFilters,
    location: Location | None = None,
    preference_context: str | None = None,
    signal_tier: str = "active",
    emit: EmitFn | None = None,
) -> ConsultResponse: ...
```

Parameter contract:
- `user_id` — required.
- `query` — required. The retrieval phrase the caller pre-rewrote from the user's message. Used directly as `discovery_filters["keyword"]`. No intent parsing, no enriched-query rewriting.
- `saved_places` — required. The list of user-saved `PlaceObject`s the caller has pre-loaded (via `RecallService.run` on the flag-off path; via `state["last_recall_results"]` on the agent path). May be empty.
- `filters` — required `ConsultFilters` object. Carries structural filters (mirrored from `PlaceObject`) plus discovery-specific fields (`radius_m`, `search_location_name`, `discovery_filters`).
- `location` — optional raw `Location`. Used as geocode bias and as the Google Places `location` when `filters.search_location_name` is null.
- `preference_context` — optional one- or two-sentence summary composed by the caller (on the agent path) or omitted (on the flag-off path). Currently unused by consult logic but reserved for future agent-driven ranking; available for future observation via Langfuse span attributes (explicitly NOT stored in reasoning_steps anymore — the field is gone).
- `signal_tier` — defaults to `"active"`. Controls warming-blend + chip-filter branches (ADR-061).
- `emit` — optional callback conforming to the `EmitFn` Protocol (`__call__(step: str, summary: str, duration_ms: float | None = None) -> None`). When supplied, `consult()` calls it at each pipeline boundary with primitive `(step_name, summary)` values matching the M5 catalog. The consult service MAY pass `duration_ms=<measured>` for branches where it wrapped the underlying call in its own timer (e.g., Google Places discovery, places-service enrichment) — and SHOULD omit `duration_ms` otherwise, letting the wrapper's closure compute it from timestamp deltas. When `emit` is not supplied, the service defaults to a no-op via `_emit = emit or (lambda _s, _m, _d=None: None)`. Services never construct `ReasoningStep` objects — the caller's closure stamps agent-layer fields.

Return contract:
- `ConsultResponse` contains `recommendation_id: str | None` and `results: list[ConsultResult]`. **The `reasoning_steps` field is removed** per the plan-doc revision; reasoning steps are delivered live via the `emit` callback, not bundled into the response.
- The `_consult_step` helper landed in feature 027 is deleted — consult service no longer constructs `ReasoningStep` objects. Every `reasoning_steps.append(...)` site in the current service body is replaced by a single `_emit(step_name, summary)` call using the catalog step names (`consult.geocode`, `consult.discover`, `consult.merge`, `consult.dedupe`, `consult.enrich`, `consult.tier_blend`, `consult.chip_filter`).

Error contract:
- Raises `NoMatchesError(query)` when the deduped + enriched + filtered candidate set is empty. Unchanged from today.
- Does NOT raise for missing filters — `ConsultFilters()` with all `None` fields is valid (means "no structural filters, default radius, no named location").
- Does NOT raise for `saved_places=[]` — discovery-only mode is a supported path.

## Main-path removals (FR-006)

Inside `consult()`, the following are deleted:
- `await self._memory.load_memories(user_id)` — memory summary now composed by the caller.
- `await self._taste_service.get_taste_profile(user_id)` on the main path — NOT the chip-filter branch, which retains this read.
- `await self._intent_parser.parse(query, user_memories=..., taste_summary=...)` — caller supplies structured `filters`.
- `await self._recall_service.run(search_query, user_id)` — caller supplies `saved_places`.
- `format_summary_for_agent(...)` / `SummaryLine` imports on the main path — caller composes `preference_context`.

## Main-path retentions

- Geocoding: `if filters.search_location_name: await self._places_client.geocode(filters.search_location_name, location_bias=<derived from location>)`.
- Radius default: `radius = filters.radius_m or get_config().consult.default_radius_m`.
- Google Places discovery (`self._places_client.discover`).
- Merge + dedupe (saved first, then discovered; dedupe by `provider_id` with `place_id` fallback).
- `self._places_service.enrich_batch(deduped, geo_only=False, priority_provider_ids=<saved set>)`.
- Validation-on-opennow branch.
- Warming-tier blend (ADR-061).
- Active-tier rejected-chip filter + confirmed-chip reasoning signal (ADR-061) — this is the ONE remaining `taste_service` read, explicitly carved out by FR-006.
- `_persist_recommendation(user_id, query, results)` (ADR-060). Signature tightened — the `reasoning_steps` argument is removed; the JSONB payload built from `ConsultResponse(...)` no longer sets that field.

## Docstring update

Replace the class docstring's "6-step pipeline" phrasing. New shape is a 4-phase pipeline: **geocode → discover → merge+dedupe → enrich+persist**. The tier-specific branches (warming blend, active chip filter) are called out in the class docstring as conditional modifiers on the enrichment output.

## mypy contract

`mypy --strict` must pass. `saved_places: list[PlaceObject]` is positional-required; calling `consult()` without it is a type error — which is the machine-checkable form of spec clarification Q2 ("no fallback inside the consult service itself").
