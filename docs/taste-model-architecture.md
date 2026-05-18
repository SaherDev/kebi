# Taste Model Architecture (ADR-058)

The taste model builds a per-user preference profile from behavioral signals. Each interaction (save, accept, reject) is logged as an append-only row. Signal counts are aggregated from all interactions, and an LLM generates a structured summary (3-6 lines) grounded in signal_counts — every item references a specific path and value in the aggregation. (The chip artifact and signal tier were removed in ADR-076.)

## Data Flow

```
User Action (save place / accept rec / reject rec)
    │
    ▼
EventDispatcher → on_taste_signal()
    │
    ▼
TasteModelService.handle_signal()
    ├── INSERT interaction row (append-only)
    └── Schedule debounced regen (30s window)
            │
            ▼ (after debounce expires)
        _run_regen(user_id)
            ├── get_interactions_with_places() → list[InteractionRow]
            ├── aggregate_signal_counts() → SignalCounts (pure, no I/O)
            ├── Min-signals guard (skip if < 3 interactions)
            ├── Stale guard (skip if log_count unchanged)
            ├── LLM call (GPT-4o-mini via provider abstraction)
            │   └── JSON mode → TasteArtifacts (summary)
            ├── validate_grounded() → drop ungrounded items
            └── upsert_regen() → persist signal_counts + summary
```

## Storage

```
taste_model (PostgreSQL, keyed by user_id):
├── signal_counts    JSONB  — structured aggregation of all interactions
├── taste_profile_summary  JSONB  — list of SummaryLine (text, signal_count, source_field, source_value)
├── generated_at     TIMESTAMPTZ
└── generated_from_log_count  INT  — stale-summary guard

interactions (PostgreSQL, append-only):
├── id         BIGSERIAL PK
├── user_id    TEXT
├── type       ENUM (save, accepted, rejected)
├── place_id   TEXT FK → places.id
└── created_at TIMESTAMPTZ
```

## Aggregation Rules

| Interaction Type | Feeds | Notes |
|-----------------|-------|-------|
| save | Main tree + source | Source counted for saves only |
| accepted | Main tree | No source tracking |
| rejected | Rejected branch | Separate from main tree |

## Signal Counts Shape

```json
{
  "totals": {"saves": N, "accepted": N, "rejected": N},
  "place_type": {"food_and_drink": N, ...},
  "subcategory": {"food_and_drink": {"restaurant": N, "cafe": N}, ...},
  "source": {"tiktok": N, "instagram": N, ...},
  "tags": {"date-spot": N, ...},
  "attributes": {
    "cuisine": {"japanese": N, ...},
    "price_hint": {"mid": N, ...},
    "ambiance": {"casual": N, ...},
    "dietary": {"vegan": N, ...},
    "good_for": {"date": N, ...},
    "location_context": {"neighborhood": {"Shibuya": N}, "city": {...}, "country": {...}}
  },
  "rejected": {"subcategory": {...}, "attributes": {...}}
}
```

## Debounce

Process-local `dict[user_id, asyncio.Task]`. Each new signal cancels the pending task and schedules a new one. Idempotent regen (full overwrite) handles rare multi-process overlap. `cancel_all()` wired to FastAPI lifespan shutdown.

## Agent Read Path

`format_summary_for_agent(lines)` joins the structured summary back to bullet text:
```
- Favors bar subcategory under food_and_drink. [8 signals]
- Primary save source is TikTok. [18 signals]
```

The agent sees readable text; structure is internal.
