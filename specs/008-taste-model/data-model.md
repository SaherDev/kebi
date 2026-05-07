# Data Model: Taste Model

**Branch**: `008-taste-model` | **Date**: 2026-03-31

---

## Entities

### TasteModel (existing table — migration required)

Represents a user's current learned taste profile. This is the nearline cache — reconstructable from the interaction log.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| id | UUID | No | — | Primary key |
| user_id | String | No | — | Unique, indexed |
| model_version | String | No | — | Version tag for the taste model algorithm |
| parameters | JSONB | No | — | 8 dimension floats (see schema below) |
| confidence | Float | No | 0.0 | Routing signal: `1 − e^(−interaction_count / 10)`. Dedicated column — not in JSONB |
| interaction_count | Integer | No | 0 | Total interactions. Drives confidence and personalization routing. Dedicated column |
| eval_score | Float | Yes | null | Written by eval pipeline only. Renamed from `performance_score` |
| created_at | Timestamp | No | now() | |
| updated_at | Timestamp | No | now() | Updated on every write |

**parameters JSONB schema** (8 named dimensions, all floats in [0, 1]):
```json
{
  "price_comfort": 0.5,
  "dietary_alignment": 0.5,
  "cuisine_frequency": 0.5,
  "ambiance_preference": 0.5,
  "crowd_tolerance": 0.5,
  "cuisine_adventurousness": 0.5,
  "time_of_day_preference": 0.5,
  "distance_tolerance": 0.5
}
```

**Migration required**: Rename `performance_score` → `eval_score`. Add `confidence` (Float, default 0.0, not null). Add `interaction_count` (Integer, default 0, not null).

---

### InteractionLog (new table — migration required)

Append-only event log. The source of truth for the taste model. Never updated after creation.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| id | UUID | No | — | Primary key |
| user_id | String | No | — | Indexed |
| signal_type | Enum | No | — | Values: save, accepted, rejected, ignored, repeat_visit, search_accepted, onboarding_explicit |
| place_id | UUID | Yes | null | FK to places.id. Null for impression-only signals (ignored) |
| gain | Float | No | — | Stored at write time from config. Changing config does not rewrite the log |
| context | JSONB | No | — | `{location, time_of_day, session_id, recommendation_id}` |
| created_at | Timestamp | No | now() | |

**signal_type enum values**:
- `save` — place saved (gain 1.0, currently wired)
- `accepted` — recommendation accepted (gain 2.0, currently wired)
- `rejected` — recommendation rejected (gain −1.5, currently wired)
- `onboarding_explicit` — taste chip confirmed or dismissed (gain ±, currently wired)
- `ignored` — recommendation shown and not acted on (deferred)
- `repeat_visit` — user revisits a saved place (deferred)
- `search_accepted` — user accepts a search result (deferred)

**FK note**: `place_id` references `places.id`. For `ignored` signals where no place_id is available, this is null. Alembic migration must define this as a nullable FK with `ON DELETE SET NULL`.

---

## ORM Changes (db/models.py)

### TasteModel — column additions

```python
# ADD:
confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
interaction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
eval_score: Mapped[float | None] = mapped_column(Float, nullable=True)

# REMOVE:
performance_score  # renamed to eval_score via migration
```

### InteractionLog — new ORM class

```python
class InteractionLog(Base):
    __tablename__ = "interaction_log"

    id: Mapped[str]             # UUID primary key
    user_id: Mapped[str]        # indexed
    signal_type: Mapped[str]    # use Python Enum; stored as string in DB
    place_id: Mapped[str | None]  # nullable FK → places.id
    gain: Mapped[float]
    context: Mapped[dict]       # JSONB
    created_at: Mapped[datetime]
```

---

## Config Additions (config/app.yaml + core/config.py)

### app.yaml additions

```yaml
taste_model:
  ema:
    price_comfort: 0.03
    dietary_alignment: 0.03
    cuisine_frequency: 0.07
    ambiance_preference: 0.07
    crowd_tolerance: 0.10
    cuisine_adventurousness: 0.10
    time_of_day_preference: 0.15
    distance_tolerance: 0.15
  signals:
    save: 1.0
    accepted: 2.0
    rejected: -1.5
    onboarding_explicit_positive: 1.2
    onboarding_explicit_negative: -0.8
    ignored: -0.5
    repeat_visit: 3.0
    search_accepted: 1.5

ranking:
  weights:
    taste_similarity: 0.40
    distance: 0.25
    price_fit: 0.20
    popularity: 0.15
```

### New Pydantic config classes (core/config.py)

```python
class TasteModelEmaConfig(BaseModel):
    price_comfort: float
    dietary_alignment: float
    cuisine_frequency: float
    ambiance_preference: float
    crowd_tolerance: float
    cuisine_adventurousness: float
    time_of_day_preference: float
    distance_tolerance: float

class TasteModelSignalsConfig(BaseModel):
    save: float
    accepted: float
    rejected: float
    onboarding_explicit_positive: float
    onboarding_explicit_negative: float
    ignored: float
    repeat_visit: float
    search_accepted: float

class TasteModelConfig(BaseModel):
    ema: TasteModelEmaConfig
    signals: TasteModelSignalsConfig

class RankingWeightsConfig(BaseModel):
    taste_similarity: float
    distance: float
    price_fit: float
    popularity: float

class RankingConfig(BaseModel):
    weights: RankingWeightsConfig

# Add to AppConfig:
taste_model: TasteModelConfig
ranking: RankingConfig
```

---

## Repository Interface

### TasteModelRepository (Protocol)

```python
class TasteModelRepository(Protocol):
    async def get_by_user_id(self, user_id: str) -> TasteModel | None: ...
    async def upsert(
        self,
        user_id: str,
        parameters: dict[str, float],
        confidence: float,
        interaction_count: int,
    ) -> TasteModel: ...
    async def log_interaction(
        self,
        user_id: str,
        signal_type: SignalType,
        place_id: str | None,
        gain: float,
        context: dict[str, Any],
    ) -> None: ...
```

Location: `src/kebi/db/repositories/taste_model_repository.py`
Exported from: `src/kebi/db/repositories/__init__.py`

---

## Personalization Routing Logic

```
get_taste_vector(user_id) → dict[str, float]:

  record = taste_repo.get_by_user_id(user_id)

  if record is None or record.interaction_count == 0:
    return DEFAULT_VECTOR  # all 0.5

  if record.interaction_count < 10:
    # Low-data blend: 40% stored / 60% defaults
    return blend(record.parameters, DEFAULT_VECTOR, personal_weight=0.40)

  return record.parameters  # full personalization
```

DEFAULT_VECTOR = `{dim: 0.5 for dim in all 8 dimensions}`

---

## State Transitions

### interaction_count lifecycle

```
0 (no record)
  → handle_place_saved / handle_recommendation_* / handle_onboarding_signal called
    → log_interaction() writes to interaction_log
    → upsert() increments interaction_count
    → confidence = 1 − e^(−new_count / 10) recomputed and written
```

### confidence curve checkpoints

| interaction_count | confidence |
|---|---|
| 0 | 0.000 |
| 5 | 0.394 |
| 10 | 0.632 |
| 20 | 0.865 |
| 30 | 0.950 |
| 50 | 0.993 |
