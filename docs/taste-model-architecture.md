# Taste Model Architecture (ADR-077)

The taste model builds a per-user preference profile from behavioral signals. Each interaction (save, accept, reject) is logged as an append-only row keyed by the shared `places_v2` catalog identity. Signal counts are aggregated from all interactions in the places_v2 vocabulary, and an LLM generates a structured summary (3-6 lines) grounded in signal_counts — every item references a specific path and value in the aggregation. (The chip artifact and signal tier were removed in ADR-076; the legacy place vocabulary was replaced in ADR-077.)

## Data Flow

```
User Action (save place / accept rec / reject rec)
    │
    ▼
EventDispatcher → on_taste_signal()
    │
    ▼
TasteModelService.handle_signal()
    ├── INSERT interaction row (append-only; place_id = places_v2.id)
    └── Schedule debounced regen (30s window)
            │
            ▼ (after debounce expires)
        _run_regen(user_id)
            ├── get_interactions() → list[RawInteraction]  (type + place_id, no JOIN)
            ├── Min-signals guard (skip if < 3 interactions)
            ├── Stale guard (skip if raw log_count unchanged)
            ├── _resolve_rows():  open session_factory →
            │     ├── PlacesSearchService.get_cores_by_ids(ids)  (DB-only, no Google)
            │     ├── UserPlacesRepo.get_by_user(user_id)        (save source)
            │     └── place_to_interaction_row(...)  (skip orphans / TTL-wiped)
            ├── aggregate_signal_counts() → SignalCounts (pure, no I/O)
            ├── LLM call (GPT-4o-mini via provider abstraction)
            │   └── JSON mode → TasteArtifacts (summary)
            ├── validate_grounded() → drop ungrounded items
            └── upsert_regen() → persist signal_counts + summary
```

Place resolution goes through the catalog's single source-of-truth service via a **DB-only** read (`get_cores_by_ids`) — no provider fallback, no cache mutation, no provider cost. This is deliberately distinct from discovery reads (`find` / `get_by_ids`) that fall back to Google. It runs inside the background regen's own short-lived session opened from `session_factory` (ADR-072: no new long-lived shared dependency).

## Storage

```
taste_model (PostgreSQL, keyed by user_id):
├── signal_counts    JSONB  — places_v2-vocabulary aggregation of all interactions
├── taste_profile_summary  JSONB  — list of SummaryLine (text, signal_count, source_field, source_value)
├── generated_at     TIMESTAMPTZ
└── generated_from_log_count  INT  — stale-summary guard (raw interaction count)

interactions (PostgreSQL, append-only):
├── id         BIGSERIAL PK
├── user_id    TEXT
├── type       ENUM (save, accepted, rejected)
├── place_id   TEXT  — places_v2.id, nullable, NO FK (survives catalog TTL-wipe)
└── created_at TIMESTAMPTZ
```

`place_id` carries the shared cross-user `places_v2.id` (one row per real place), which is what makes future "users with similar taste" collaboration possible. No FK: accepted/rejected recs may reference places never in `user_places`, and a CASCADE FK would destroy behavioral history when the nightly catalog wipe removes a place. Pre-cutover orphan rows (legacy place ids) were purged once by migration `c7d8e9f0a1b2`.

## Aggregation Rules

| Interaction Type | Feeds | Notes |
|-----------------|-------|-------|
| save | Main tree + source | Source from `user_places.source`, saves only |
| accepted | Main tree | No source tracking |
| rejected | Rejected branch | Separate from main tree |

`source` is the per-user save provenance (`UserPlace.source`), not a property of the shared place. Categories, typed tags, and location come from the shared `PlaceCore`.

## Signal Counts Shape

```json
{
  "totals": {"saves": N, "accepted": N, "rejected": N},
  "categories": {"restaurant": N, "bar": N, ...},
  "source": {"tiktok": N, "instagram": N, ...},
  "tags": {
    "cuisine": {"japanese": N, ...},
    "dietary": {"vegan": N, ...},
    "feature": {"outdoor_seating": N, ...},
    "atmosphere": {"cozy": N, ...},
    "service": {"serves_brunch": N, ...},
    "price": {"moderate": N, ...},
    "accessibility": {"wheelchair_entrance": N, ...},
    "time": {"late_night": N, ...},
    "season": {"summer": N, ...}
  },
  "location": {"neighborhood": {"Shibuya": N}, "city": {...}, "country": {...}},
  "rejected": {"categories": {...}, "tags": {...}, "location": {...}}
}
```

Every container stays a nested `dict[str, int]` so the grounding validator's dotted-path walk (`regen._resolve_path`) resolves `source_field` values like `categories`, `tags.price`, `location.city`, `rejected.categories` unchanged.

## Debounce

Process-local `dict[user_id, asyncio.Task]`. Each new signal cancels the pending task and schedules a new one. Idempotent regen (full overwrite) handles rare multi-process overlap. `cancel_all()` wired to FastAPI lifespan shutdown.

## Agent Read Path

`format_summary_for_agent(lines)` joins the structured summary back to bullet text:
```
- Favors bar category. [8 signals]
- Primary save source is TikTok. [18 signals]
```

The agent sees readable text; structure is internal. `signal_counts` is never exposed externally — no API contract change.
