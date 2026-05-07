# Data Model: 022-recommendations-context-signals

**Date**: 2026-04-17

## Entity: Recommendation (renamed from ConsultLog)

**Table**: `recommendations` (renamed from `consult_logs`)

| Column      | Type                  | Constraints                    | Notes                                |
|-------------|-----------------------|--------------------------------|--------------------------------------|
| id          | UUID                  | PK, default uuid4             | Database-generated, returned as recommendation_id |
| user_id     | String                | NOT NULL, indexed              | Clerk-issued user ID                 |
| query       | Text                  | NOT NULL                       | Original user query text             |
| response    | JSONB                 | NOT NULL                       | Full ConsultResponse serialized via model_dump(mode="json") |
| created_at  | DateTime(timezone=True)| NOT NULL, server_default now()| Auto-timestamp                       |

**ORM Model**: `Recommendation` (renamed from `ConsultLog`) in `src/kebi/db/models.py`

**Repository**: `RecommendationRepository` protocol + `SQLAlchemyRecommendationRepository` + `NullRecommendationRepository`

**Methods**:
- `save(recommendation: Recommendation) -> None` — insert row, commit
- `exists(recommendation_id: str) -> bool` — `SELECT 1 WHERE id = :id LIMIT 1`

**Migration**: `ALTER TABLE consult_logs RENAME TO recommendations` + rename index

---

## Entity: UserContext (read-only, not persisted)

Assembled from `TasteProfile` returned by `TasteModelService.get_taste_profile(user_id)`.

| Field            | Source                                  | Type            |
|------------------|-----------------------------------------|-----------------|
| saved_places_count | TasteProfile.signal_counts["totals"]["saves"] | int        |
| chips            | TasteProfile.chips (pass-through)       | list[Chip]      |

**Cold start** (no taste profile): `saved_places_count = 0`, `chips = []`

---

## Entity: Signal (request-only, not persisted as own table)

Signals are dispatched via EventDispatcher → EventHandlers.on_taste_signal → writes to `interaction_log` table (existing).

| Field             | Type   | Required | Notes                                    |
|-------------------|--------|----------|------------------------------------------|
| signal_type       | Literal["recommendation_accepted", "recommendation_rejected"] | Yes | Discriminated union |
| user_id           | str    | Yes      | Clerk-issued user ID                     |
| recommendation_id | str    | Yes      | Must exist in recommendations table      |
| place_id          | str    | Yes      | Trusted, not validated against places    |

---

## Relationships

```
Recommendation (1) ←--referenced-by--→ (N) Signal
  └── recommendation_id validates signal_type requests
  └── No FK constraint (signal writes to interaction_log, not recommendations)

TasteProfile (1) ←--read-by--→ UserContext
  └── signal_counts.totals.saves → saved_places_count
  └── chips → chips (pass-through)
```
