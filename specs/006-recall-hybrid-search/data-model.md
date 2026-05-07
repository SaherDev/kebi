# Data Model: Recall Hybrid Search

**Branch**: `006-recall-hybrid-search` | **Date**: 2026-03-31

## Existing Tables (read-only for this feature)

No new Alembic migration required. This feature reads from existing `places` and `embeddings` tables.

### `places` (existing)

| Column | Type | Nullable | Notes |
|---|---|---|---|
| id | String (UUID) | No | Primary key |
| user_id | String | No | Indexed — used to scope all recall queries |
| place_name | String | No | FTS field |
| address | String | No | Returned in response |
| cuisine | String | Yes | FTS field (COALESCE to '' when NULL) |
| price_range | String | Yes | Returned in response |
| source_url | Text | Yes | Returned in response |
| created_at | DateTime(tz) | No | Mapped to `saved_at` in response |

### `embeddings` (existing)

| Column | Type | Nullable | Notes |
|---|---|---|---|
| id | String (UUID) | No | Primary key |
| place_id | String (FK) | No | Indexed — join key for vector search |
| vector | Vector(1024) | No | Cosine similarity via `<=>` operator |
| model_name | String | No | Filter by `voyage-4-lite` if needed |

## Pydantic Schemas (new: `src/kebi/api/schemas/recall.py`)

### `RecallRequest`

| Field | Type | Validation | Notes |
|---|---|---|---|
| query | str | min_length=1 | Natural language query; empty triggers 400 |
| user_id | str | required | Injected by NestJS from auth token |

### `RecallResult`

| Field | Type | Nullable | Source |
|---|---|---|---|
| place_id | str | No | `places.id` |
| place_name | str | No | `places.place_name` |
| address | str | No | `places.address` |
| cuisine | str \| None | Yes | `places.cuisine` |
| price_range | str \| None | Yes | `places.price_range` |
| source_url | str \| None | Yes | `places.source_url` |
| saved_at | datetime | No | `places.created_at` |
| match_reason | str | No | Derived from search method(s) — see below |

### `RecallResponse`

| Field | Type | Default | Notes |
|---|---|---|---|
| results | list[RecallResult] | — | Ordered by RRF score descending |
| total | int | — | Always equals `len(results)` (post-limit count) |
| empty_state | bool | False | True only when user has zero saved places |

## `match_reason` Derivation Logic

| Matched by | `match_reason` string |
|---|---|
| Vector + Text | `"Matched by name, cuisine, and semantic similarity"` |
| Vector only | `"Matched by semantic similarity"` |
| Text only | `"Matched by name or cuisine"` |
| Text only (embedding failed) | `"Matched by name or cuisine (semantic unavailable)"` |

## Config Addition (`config/app.yaml`)

```yaml
recall:
  max_results: 10    # default result limit; configurable without code changes
  rrf_k: 60          # RRF constant (Cormack et al. 2009)
  candidate_multiplier: 2  # pre-fetch N × max_results candidates before RRF merge
```

## AppConfig Extension (`src/kebi/core/config.py`)

New `RecallConfig` Pydantic model:
```
RecallConfig:
  max_results: int = 10
  rrf_k: int = 60
  candidate_multiplier: int = 2
```
Added as `recall: RecallConfig` field on `AppConfig`.
