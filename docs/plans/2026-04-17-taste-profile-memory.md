# Taste Profile & Memory Redesign

## Goal

Replace the EMA-based 8-dimensional taste model with **signal_counts** (pure Python aggregation from interactions) + **taste_profile_summary** (LLM-generated bullet list) + **chips** (LLM-generated UI labels grounded in signal_counts). Delete all EMA calculation logic. Simplify the interactions table. Memory (user_memories) is unchanged structurally.

## Decisions

- **Ranking**: Delete `RankingService` entirely. The agent LLM (Claude Sonnet) ranks candidates via reasoning over `taste_profile_summary` + `signal_counts` + place data. No numeric scoring formula. See ADR-058 (Step 1 of this plan).
- **Debounce**: asyncio tasks with cancellation (process-local `dict[user_id, Task]`). Idempotent regen handles rare multi-process overlap. Shutdown hook cancels in-flight tasks.
- **signal_counts update**: Full overwrite from fresh aggregation each regen. Interactions is append-only so counts only grow.
- **Prompt template**: Lives in a dedicated file (`config/prompts/taste_regen.txt`), not hardcoded in Python.
- **Chips**: Generated alongside summary in a single LLM call. Validated against signal_counts before writing. Each chip is a short UI label grounded in a specific signal_counts path and value.

---

## Step 1: ADR-058 — Delete RankingService, replace with agent-driven ranking

### `docs/decisions.md` — add at top

```
## ADR-058: Replace numeric RankingService with agent-driven ranking

**Date:** 2026-04-17
**Status:** accepted
**Context:** The existing RankingService uses an 8-dimensional EMA taste vector
for 40% of its scoring weight (weighted Euclidean distance). The EMA dimensions
(price_comfort, dietary_alignment, etc.) are opaque — they don't map cleanly to
user preferences and can't be inspected or explained. Replacing the taste model
with signal_counts + taste_profile_summary (ADR pending) makes the numeric
taste_similarity score impossible to compute. Rather than invent a new numeric
proxy from signal_counts, we move ranking to the agent LLM which can reason
over the full taste profile in natural language.

**Decision:** Delete RankingService. The agent (not yet built) will handle
selection directly from enriched candidates using taste_profile_summary,
signal_counts, user_memories, and place data. Until the agent is built,
ConsultService returns enriched candidates unranked (saved first, then
discovered) — ranking is deferred, not solved.

Revisit if first-recommendation acceptance rate shows agent-only selection
is insufficient. The three-layer design (hard filters + scoring + agent)
is the fallback — a lightweight numeric ranker can be reintroduced as a
pre-filter without re-adding the EMA machinery.

**Cold start (no taste_profile_summary):** Agent sees only user_memories and
candidate place data. No personalization signal, which is correct for a
new user.

**Evaluation:** With numeric scoring we A/B-tested weight changes. With
agent ranking we A/B-test prompts. Langfuse traces attach the exact
taste_profile_summary and user_memories used in each decision for
prompt-level attribution.

**LLM call ownership:** The agent owns all runtime LLM calls (intent parsing,
orchestration, ranking). The taste regen job is a background process triggered
by domain events — it calls GPT-4o-mini to generate taste_profile_summary
outside the agent's reasoning loop. This is not an agent call; it is a
pre-computation step that populates a cache the agent reads at session start.
Same pattern as embedding generation (Voyage call triggered by PlaceSaved,
consumed by RecallService at query time).

**Consequences:** No deterministic ranking until the agent is built. Consult
returns candidates in source order (saved first). The ranking config block
in app.yaml is deleted. RankingWeightsConfig and RankingConfig are deleted
from config.py.
```

---

## Step 2: Config cleanup

### `config/app.yaml`

Delete the entire `taste_model:` block (ema, signals, observations). Replace with:

```yaml
# --------------------------------------------------------------------------
# Taste Model Configuration
# Signal-counts aggregation + LLM-generated taste_profile_summary + chips
# --------------------------------------------------------------------------
taste_model:
  debounce_window_seconds: 30
  regen:
    min_signals: 3 # skip regen if total interactions < 3
    early_signal_threshold: 10 # prefix lines with "Early signal:" below this
```

Add `taste_regen` model role under `models:`:

```yaml
taste_regen:
  provider: openai
  model: gpt-4o-mini
  max_tokens: 512
  temperature: 0.3
```

Delete the entire `ranking:` block (RankingService is deleted per ADR-058).

### `config/prompts/taste_regen.txt` — new

System prompt for taste_profile_summary + chips generation. Kept in a file, not hardcoded.

```
You generate two artifacts from a user's behavioral signal counts:
1. summary — 3 to 6 bullet lines describing patterns
2. chips — 3 to 6 short display labels for the UI

Input is JSON following this shape:
{
  "totals": {"saves": int, "accepted": int, "rejected": int, "onboarding_confirmed": int, "onboarding_dismissed": int},
  "place_type": {"<place_type>": count},
  "subcategory": {"<place_type>": {"<subcategory>": count}},
  "source": {"<source>": count},
  "tags": {"<tag>": count},
  "attributes": {
    "cuisine": {"<value>": count},
    "price_hint": {"<value>": count},
    "ambiance": {"<value>": count},
    "dietary": {"<value>": count},
    "good_for": {"<value>": count},
    "location_context": {
      "neighborhood": {"<value>": count},
      "city": {"<value>": count},
      "country": {"<value>": count}
    }
  },
  "rejected": {
    "subcategory": {"<place_type>": {"<subcategory>": count}},
    "attributes": { same shape as attributes }
  }
}

Rules for summary:
- Each summary item has: text (the human-readable pattern), signal_count (integer), source_field (JSON path in the input), source_value (the value at that path, or null for aggregate claims).
- signal_count must appear verbatim in the input JSON. Do not invent numbers.
- Use exact field values from signal_counts (subcategory names, cuisine names, source names).
- If totals.saves < {early_signal_threshold}, prefix each text with "Early signal:".
- One pattern per item. No preamble in text fields.

Rules for chips:
- Each chip is a short label for a UI button (2 to 4 words).
- Chip must be grounded in signal_counts — source_field is the JSON path, source_value is the value at that path.
- Cover multiple place types when data supports it (food, things to do, shopping, services, accommodation).
- Skip any field with signal_count < 3.
- Order chips by strongest signal first.

Output strict JSON. No markdown, no prose wrapper.

Example 1 — food and nightlife user:
INPUT:
{"totals": {"saves": 28, "accepted": 11, "rejected": 6, "onboarding_confirmed": 0, "onboarding_dismissed": 0}, "place_type": {"food_and_drink": 24, "things_to_do": 3, "accommodation": 1}, "subcategory": {"food_and_drink": {"bar": 8, "restaurant": 6, "cafe": 5, "bakery": 3}, "things_to_do": {"nightlife": 2}}, "source": {"tiktok": 18, "instagram": 6, "manual": 4}, "attributes": {"cuisine": {"izakaya": 8, "ramen": 6}, "price_hint": {"low": 9, "mid": 15, "high": 4}, "location_context": {"neighborhood": {"Shibuya": 9, "Shimokitazawa": 5}}}, "rejected": {"subcategory": {"accommodation": {"hotel": 3}}}}

OUTPUT:
{"summary": [{"text": "Favors bar subcategory under food_and_drink.", "signal_count": 8, "source_field": "subcategory.food_and_drink", "source_value": "bar"}, {"text": "Primary save source is TikTok.", "signal_count": 18, "source_field": "source", "source_value": "tiktok"}, {"text": "Prefers mid price_hint over high.", "signal_count": 15, "source_field": "attributes.price_hint", "source_value": "mid"}, {"text": "Saves cluster in Shibuya neighborhood.", "signal_count": 9, "source_field": "attributes.location_context.neighborhood", "source_value": "Shibuya"}, {"text": "Rejects hotel accommodation.", "signal_count": 3, "source_field": "rejected.subcategory.accommodation", "source_value": "hotel"}], "chips": [{"label": "Izakaya lover", "source_field": "attributes.cuisine", "source_value": "izakaya", "signal_count": 8}, {"label": "Bar enthusiast", "source_field": "subcategory.food_and_drink", "source_value": "bar", "signal_count": 8}, {"label": "Shibuya regular", "source_field": "attributes.location_context.neighborhood", "source_value": "Shibuya", "signal_count": 9}, {"label": "Finds places on TikTok", "source_field": "source", "source_value": "tiktok", "signal_count": 18}, {"label": "Budget-friendly", "source_field": "attributes.price_hint", "source_value": "mid", "signal_count": 15}]}

Example 2 — museum traveler:
INPUT:
{"totals": {"saves": 67, "accepted": 22, "rejected": 3, "onboarding_confirmed": 0, "onboarding_dismissed": 0}, "place_type": {"things_to_do": 58, "food_and_drink": 9}, "subcategory": {"things_to_do": {"museum": 28, "cultural_site": 22, "experience": 8}, "food_and_drink": {"restaurant": 9}}, "source": {"manual": 52, "link": 15}, "attributes": {"price_hint": {"mid": 40, "high": 18}, "location_context": {"city": {"Rome": 15, "Paris": 14, "Madrid": 12, "Vienna": 10, "Berlin": 8}}}, "rejected": {}}

OUTPUT:
{"summary": [{"text": "Favors museum subcategory under things_to_do.", "signal_count": 28, "source_field": "subcategory.things_to_do", "source_value": "museum"}, {"text": "Favors cultural_site subcategory under things_to_do.", "signal_count": 22, "source_field": "subcategory.things_to_do", "source_value": "cultural_site"}, {"text": "Prefers mid price_hint over high.", "signal_count": 40, "source_field": "attributes.price_hint", "source_value": "mid"}, {"text": "Saves cluster in Rome, Paris, Madrid.", "signal_count": 41, "source_field": "totals", "source_value": null}], "chips": [{"label": "Museum enthusiast", "source_field": "subcategory.things_to_do", "source_value": "museum", "signal_count": 28}, {"label": "Cultural sites", "source_field": "subcategory.things_to_do", "source_value": "cultural_site", "signal_count": 22}, {"label": "Rome regular", "source_field": "attributes.location_context.city", "source_value": "Rome", "signal_count": 15}, {"label": "Premium taste", "source_field": "attributes.price_hint", "source_value": "mid", "signal_count": 40}]}

Example 3 — sparse user:
INPUT:
{"totals": {"saves": 3, "accepted": 0, "rejected": 0, "onboarding_confirmed": 1, "onboarding_dismissed": 0}, "place_type": {"food_and_drink": 4}, "subcategory": {"food_and_drink": {"cafe": 3, "restaurant": 1}}, "source": {"instagram": 3}, "attributes": {"cuisine": {"coffee": 3}}, "rejected": {}}

OUTPUT:
{"summary": [{"text": "Early signal: cafe subcategory under food_and_drink.", "signal_count": 3, "source_field": "subcategory.food_and_drink", "source_value": "cafe"}, {"text": "Early signal: primary save source is Instagram.", "signal_count": 3, "source_field": "source", "source_value": "instagram"}, {"text": "Early signal: coffee cuisine preference.", "signal_count": 3, "source_field": "attributes.cuisine", "source_value": "coffee"}], "chips": [{"label": "Coffee lover", "source_field": "attributes.cuisine", "source_value": "coffee", "signal_count": 3}, {"label": "Cafe enthusiast", "source_field": "subcategory.food_and_drink", "source_value": "cafe", "signal_count": 3}]}

Example 4 — shopping user:
INPUT:
{"totals": {"saves": 52, "accepted": 20, "rejected": 2, "onboarding_confirmed": 0, "onboarding_dismissed": 0}, "place_type": {"shopping": 44, "food_and_drink": 8}, "subcategory": {"shopping": {"boutique": 18, "market": 14, "specialty_store": 8, "bookstore": 4}, "food_and_drink": {"cafe": 8}}, "source": {"tiktok": 30, "instagram": 18}, "attributes": {"price_hint": {"low": 25, "mid": 20}, "location_context": {"neighborhood": {"Shimokitazawa": 15, "Harajuku": 10}}}, "rejected": {}}

OUTPUT:
{"summary": [{"text": "Favors boutique subcategory under shopping.", "signal_count": 18, "source_field": "subcategory.shopping", "source_value": "boutique"}, {"text": "Favors market subcategory under shopping.", "signal_count": 14, "source_field": "subcategory.shopping", "source_value": "market"}, {"text": "Primary save source is TikTok.", "signal_count": 30, "source_field": "source", "source_value": "tiktok"}, {"text": "Prefers low price_hint over mid.", "signal_count": 25, "source_field": "attributes.price_hint", "source_value": "low"}, {"text": "Saves cluster in Shimokitazawa neighborhood.", "signal_count": 15, "source_field": "attributes.location_context.neighborhood", "source_value": "Shimokitazawa"}], "chips": [{"label": "Boutique shopper", "source_field": "subcategory.shopping", "source_value": "boutique", "signal_count": 18}, {"label": "Market lover", "source_field": "subcategory.shopping", "source_value": "market", "signal_count": 14}, {"label": "Budget-friendly", "source_field": "attributes.price_hint", "source_value": "low", "signal_count": 25}, {"label": "Shimokitazawa regular", "source_field": "attributes.location_context.neighborhood", "source_value": "Shimokitazawa", "signal_count": 15}, {"label": "Finds places on TikTok", "source_field": "source", "source_value": "tiktok", "signal_count": 30}]}
```

### `src/kebi/core/config.py`

Delete: `TasteModelEmaConfig`, `TasteModelSignalsConfig`, `TasteModelObservationsConfig`, `RankingWeightsConfig`, `RankingConfig`.

Replace `TasteModelConfig`:

```python
class TasteRegenConfig(BaseModel):
    min_signals: int = 3
    early_signal_threshold: int = 10

class TasteModelConfig(BaseModel):
    debounce_window_seconds: int = 30
    regen: TasteRegenConfig = TasteRegenConfig()
```

Remove `ranking: RankingConfig` from `AppConfig`. Change `taste_model` to have a default: `taste_model: TasteModelConfig = TasteModelConfig()`.

---

## Step 3: Database models

### `src/kebi/db/models.py`

Replace `SignalType` enum:

```python
class InteractionType(PyEnum):
    SAVE = "save"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ONBOARDING_CONFIRM = "onboarding_confirm"
    ONBOARDING_DISMISS = "onboarding_dismiss"
```

Replace `InteractionLog` with `Interaction`:

```python
class Interaction(Base):
    __tablename__ = "interactions"
    __table_args__ = (
        Index("ix_interactions_user_type", "user_id", "type"),
        Index("ix_interactions_user_created", "user_id", "created_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[InteractionType] = mapped_column(...)
    place_id: Mapped[str] = mapped_column(String, ForeignKey("places.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

Replace `TasteModel`:

```python
class TasteModel(Base):
    __tablename__ = "taste_model"
    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    taste_profile_summary: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    signal_counts: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    chips: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    generated_from_log_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
```

---

## Step 4: Alembic migration

Single migration, two phases:

**Phase A — interactions reshape:**

1. Map `onboarding_explicit` rows: read `context->>'confirmed'`, update `signal_type` to `onboarding_confirm` or `onboarding_dismiss`
2. Delete rows where `place_id IS NULL`
3. Drop `gain`, `context` columns
4. Rename table `interaction_log` -> `interactions`
5. Rename column `signal_type` -> `type`
6. Replace enum: drop old values (`ignored`, `repeat_visit`, `search_accepted`, `onboarding_explicit`), add new (`onboarding_confirm`, `onboarding_dismiss`)
7. Alter `place_id` to NOT NULL
8. Drop UUID PK, add BIGSERIAL PK
9. Add composite indexes: `(user_id, type)`, `(user_id, created_at)`

**Phase B — taste_model reshape:**

1. Drop columns: `id`, `model_version`, `parameters`, `confidence`, `interaction_count`, `eval_score`, `created_at`, `updated_at`
2. Change PK to `user_id` (drop old PK on `id`, drop unique index on `user_id`, add PK on `user_id`)
3. Add columns: `taste_profile_summary` (JSONB NOT NULL DEFAULT '[]'), `signal_counts` (JSONB NOT NULL DEFAULT '{}'), `chips` (JSONB NOT NULL DEFAULT '[]'), `generated_at` (TIMESTAMPTZ), `generated_from_log_count` (INT NOT NULL DEFAULT 0)

---

## Step 5: Repository layer

### `src/kebi/db/repositories/taste_model_repository.py` — rewrite

Protocol — returns Pydantic `InteractionRow`, not SQLAlchemy `Row`:

```python
class TasteModelRepository(Protocol):
    async def get_by_user_id(self, user_id: str) -> TasteModel | None: ...
    async def upsert_regen(self, user_id: str, signal_counts: dict, summary: list[dict], chips: list[dict], log_count: int) -> None: ...
    async def log_interaction(self, user_id: str, interaction_type: InteractionType, place_id: str) -> None: ...
    async def get_interactions_with_places(self, user_id: str) -> list[InteractionRow]: ...
    async def count_interactions(self, user_id: str) -> int: ...
```

Implementation handles `Row -> InteractionRow` conversion internally:

- `log_interaction`: INSERT into `interactions` (type, user_id, place_id — no gain, no context)
- `upsert_regen`: INSERT ... ON CONFLICT (user_id) DO UPDATE SET signal_counts, taste_profile_summary, chips, generated_at=now(), generated_from_log_count
- `get_interactions_with_places`: SELECT + JOIN, then convert each Row to `InteractionRow` with `PlaceAttributes(**row.attributes)` hydration
- `count_interactions`: SELECT COUNT(\*)

---

## Step 6: Signal counts aggregation + schemas

### `src/kebi/core/taste/schemas.py` — new

Aligned with `PlaceObject` and `PlaceAttributes` from `core/places/models.py`:

```python
class InteractionRow(BaseModel):
    """Typed shape for the interactions JOIN places query result.
    Fields mirror PlaceObject Tier 1 columns exactly."""
    type: str                                      # InteractionType value
    place_type: str                                # PlaceType enum value
    subcategory: str | None                        # validated subcategory or None
    source: str | None                             # PlaceSource enum value or None
    tags: list[str] = Field(default_factory=list)  # mirrors PlaceObject.tags
    attributes: PlaceAttributes = Field(default_factory=PlaceAttributes)
    # attributes reuses PlaceAttributes directly:
    #   cuisine, price_hint, ambiance, dietary, good_for, location_context

class SummaryLine(BaseModel):
    text: str = Field(min_length=1, max_length=200)
    signal_count: int
    source_field: str       # path in signal_counts that backs this claim
    source_value: str | None  # null for aggregate claims like total saves

class Chip(BaseModel):
    label: str = Field(min_length=1, max_length=30)
    source_field: str       # path in signal_counts, e.g. "attributes.cuisine"
    source_value: str       # value at that path, e.g. "izakaya"
    signal_count: int

class TasteArtifacts(BaseModel):
    summary: list[SummaryLine] = Field(max_length=6)
    chips: list[Chip] = Field(max_length=8)

class TasteProfile(BaseModel):
    taste_profile_summary: list[SummaryLine] = Field(default_factory=list)
    signal_counts: dict[str, Any]
    chips: list[Chip] = Field(default_factory=list)
```

The `attributes` field reuses `PlaceAttributes` from `core/places/models.py` — no parallel definition. The repository hydrates it from the JSONB column via `PlaceAttributes(**row.attributes)` when not None.

### `src/kebi/core/taste/aggregation.py` — new

Pydantic models for signal_counts shape + pure `aggregate_signal_counts()` function.

Positive types (`save`, `accepted`, `onboarding_confirm`) feed main tree.
Negative types (`rejected`, `onboarding_dismiss`) feed `rejected` branch.
`source` aggregation is save-only.

```python
class TotalCounts(BaseModel):
    saves: int = 0
    accepted: int = 0
    rejected: int = 0
    onboarding_confirmed: int = 0
    onboarding_dismissed: int = 0

class LocationContextCounts(BaseModel):
    neighborhood: dict[str, int] = {}
    city: dict[str, int] = {}
    country: dict[str, int] = {}

class AttributeCounts(BaseModel):
    cuisine: dict[str, int] = {}
    price_hint: dict[str, int] = {}
    ambiance: dict[str, int] = {}
    dietary: dict[str, int] = {}
    good_for: dict[str, int] = {}
    location_context: LocationContextCounts = LocationContextCounts()

class RejectedCounts(BaseModel):
    subcategory: dict[str, dict[str, int]] = {}
    attributes: AttributeCounts = AttributeCounts()

class SignalCounts(BaseModel):
    totals: TotalCounts = TotalCounts()
    place_type: dict[str, int] = {}
    subcategory: dict[str, dict[str, int]] = {}
    source: dict[str, int] = {}
    tags: dict[str, int] = {}
    attributes: AttributeCounts = AttributeCounts()
    rejected: RejectedCounts = RejectedCounts()

def aggregate_signal_counts(rows: list[InteractionRow]) -> SignalCounts:
    """Pure function. No I/O."""
```

---

## Step 7: Debounce mechanism

### `src/kebi/core/taste/debounce.py` — new

```python
class RegenDebouncer:
    _pending: dict[str, asyncio.Task[None]]

    def schedule(self, user_id: str, coro_factory: Callable[[], Awaitable[None]], delay_seconds: float) -> None:
        """Cancel existing task for user_id if pending, schedule new delayed task."""

    async def cancel_all(self) -> None:
        """Called from FastAPI lifespan shutdown. Cancels all in-flight tasks."""
        for task in self._pending.values():
            task.cancel()
        await asyncio.gather(*self._pending.values(), return_exceptions=True)
        self._pending.clear()
```

Module-level singleton. Wire `cancel_all()` into the FastAPI lifespan shutdown hook in `api/main.py`.

---

## Step 8: Taste service rewrite

### `src/kebi/core/taste/service.py` — rewrite

Delete all EMA logic: `TASTE_DIMENSIONS`, `DEFAULT_VECTOR`, `_apply_taste_update`, `_place_to_metadata`, `_get_observation_value`, `_blend_vectors`.

```python
class TasteModelService:
    def __init__(self, session: AsyncSession) -> None: ...

    async def handle_signal(self, user_id: str, signal_type: InteractionType, place_id: str) -> None:
        """Write interaction row, schedule debounced regen."""

    async def get_taste_profile(self, user_id: str) -> TasteProfile | None:
        """Read taste_model row. No LLM call."""

    async def _run_regen(self, user_id: str) -> None:
        """Read interactions -> aggregate -> LLM artifacts -> validate chips -> write."""
```

**`_run_regen` details:**

1. `rows = await self._repo.get_interactions_with_places(user_id)` — returns `list[InteractionRow]`
2. **Min-signals guard**: `if len(rows) < self._config.regen.min_signals: return` — skip if < 3 interactions
3. `signal_counts = aggregate_signal_counts(rows)` — pure Python
4. Check `generated_from_log_count` vs `len(rows)` — skip if equal (no new signals)
5. Load prompt template from `config/prompts/taste_regen.txt`, inject `early_signal_threshold`
6. Call GPT-4o-mini via provider abstraction (`get_llm("taste_regen")`) with JSON mode / structured output → parse into `TasteArtifacts`
7. Parse failure retries once; second failure skips regen entirely
8. `artifacts = validate_grounded(artifacts, signal_counts)` — drop invalid summary lines and chips
9. **Langfuse trace**: input = `signal_counts`, output = `taste_profile_summary` + `chips`, metadata = `{user_id, log_row_count, prior_log_count, debounce_window_ms, dropped_summary_count, dropped_chip_count, dropped_items}`
10. `upsert_regen(user_id, signal_counts, summary_lines, chips, len(rows))`

**Observability (per spec):**

- Each `_run_regen` call = one Langfuse trace
- Regen latency metric (p50, p99) emitted from trace duration
- Regen call count logged per completion
- `generated_from_log_count` persisted on every regen for stale-summary detection
- Dropped chips logged in Langfuse metadata

### `src/kebi/core/taste/regen.py` — new

Prompt builder — loads template from `config/prompts/taste_regen.txt` (not hardcoded):

```python
def build_regen_messages(signal_counts: SignalCounts, early_signal_threshold: int) -> list[dict[str, str]]:
    """Build system + user messages for taste artifact generation.
    System prompt loaded from config/prompts/taste_regen.txt.
    User message is JSON dump of signal_counts."""

def load_regen_prompt_template() -> str:
    """Load prompt from config/prompts/taste_regen.txt."""

def validate_grounded(artifacts: TasteArtifacts, signal_counts: SignalCounts) -> TasteArtifacts:
    """Validate both summary lines and chips against signal_counts.
    Drop any item whose source_field path does not exist in signal_counts
    or whose source_value does not appear at that path.
    Log drops via Langfuse metadata {dropped_summary_count, dropped_chip_count, dropped_items}.
    Surviving items get written. No retry."""

def format_summary_for_agent(lines: list[SummaryLine]) -> str:
    """Join structured summary back to bullet-point text for agent prompt injection.
    Agent sees the same readable format regardless of storage structure."""
```

---

## Step 9: Event handlers update

### `src/kebi/core/events/handlers.py` — simplify

All taste handlers become thin wrappers calling `handle_signal`:

- `on_place_saved`: for each place_id -> `handle_signal(user_id, SAVE, place_id)`
- `on_recommendation_accepted`: `handle_signal(user_id, ACCEPTED, place_id)`
- `on_recommendation_rejected`: `handle_signal(user_id, REJECTED, place_id)`
- `on_onboarding_signal`: map `confirmed` bool -> `ONBOARDING_CONFIRM` / `ONBOARDING_DISMISS`

Domain events (`events.py`) unchanged.

---

## Step 10: Delete RankingService + update ConsultService

### `src/kebi/core/ranking/service.py` — DELETE entire file

### `src/kebi/core/ranking/__init__.py` — clean up exports

### `src/kebi/core/consult/service.py`

- Remove `RankingService` import and constructor param
- Remove `TasteModelService` import and constructor param
- Remove `taste_vector = await self._taste_service.get_taste_vector(user_id)` line
- Remove `ranked = self._ranking_service.rank(...)` call and related scoring logic
- Return enriched candidates unranked (sorted by source: saved first, then discovered). No ranking until the agent is built — acceptable for now.

### `src/kebi/core/consult/types.py`

- Remove `ScoredPlace` if it only existed for ranking output
- Keep `ConsultResult` but remove `confidence` score field (or repurpose)

### `src/kebi/api/deps.py`

- Remove `RankingService()` from `get_consult_service` wiring
- Remove `taste_service` from `get_consult_service` wiring

---

## Step 11: Cleanup + docs

- Rewrite `docs/taste-model-architecture.md` for the new system
- Update `CLAUDE.md` Recent Changes

---

## Step 12: Tests

- `tests/core/taste/test_aggregation.py` — unit tests for `aggregate_signal_counts` with various row combos
- `tests/core/taste/test_service.py` — rewrite for `handle_signal` + `_run_regen` + min-signals guard
- `tests/core/taste/test_debounce.py` — cancellation behavior + `cancel_all` shutdown
- `tests/core/taste/test_regen.py` — prompt builder test asserting signal_counts numbers appear verbatim in the prompt (the "LLM must reuse counts" guarantee — verify the input side)
- `tests/core/taste/test_validation.py` — validate_grounded covers both SummaryLine and Chip: valid item passes, bad source_field drops, mismatched source_value drops, all items dropped triggers warning log, SummaryLine with null source_value for aggregate claims passes
- `tests/core/ranking/` — DELETE (RankingService deleted)
- `tests/core/events/test_handlers.py` — handlers call `handle_signal`

---

## Verification

- [ ] `poetry run alembic upgrade head` — migration runs clean
- [ ] `poetry run pytest tests/core/taste/ -x`
- [ ] `poetry run pytest tests/core/events/ -x`
- [ ] `poetry run ruff check src/ tests/`
- [ ] `poetry run mypy src/`
- [ ] Manual: save triggers interaction row + debounced regen + taste_profile_summary + chips populated
- [ ] Manual: Langfuse trace for regen shows signal_counts input, summary + chips output, dropped chip metadata
- [ ] Manual: FastAPI shutdown cancels in-flight debounce tasks cleanly

---

## Files

| File                                                      | Action                                                                                                              |
| --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `docs/decisions.md`                                       | Add ADR-058 (delete RankingService, agent-driven ranking)                                                           |
| `docs/taste-model-architecture.md`                        | Rewrite for new system                                                                                              |
| `config/app.yaml`                                         | Delete EMA block, delete ranking block, add taste_regen role, add regen config                                      |
| `config/prompts/taste_regen.txt`                          | New — system prompt template for summary + chips generation                                                         |
| `src/kebi/core/config.py`                            | Delete 3 EMA config classes + RankingWeightsConfig + RankingConfig, add TasteRegenConfig, simplify TasteModelConfig |
| `src/kebi/db/models.py`                              | Replace SignalType->InteractionType, InteractionLog->Interaction, reshape TasteModel (add chips column)             |
| `alembic/versions/XXXX_*.py`                              | New migration                                                                                                       |
| `src/kebi/db/repositories/taste_model_repository.py` | Rewrite — returns `list[InteractionRow]` not `list[Row]`, upsert takes chips param                                  |
| `src/kebi/core/taste/service.py`                     | Rewrite — delete EMA, add handle_signal + regen with artifacts + chip validation                                    |
| `src/kebi/core/taste/aggregation.py`                 | New — SignalCounts model + aggregate function                                                                       |
| `src/kebi/core/taste/schemas.py`                     | New — InteractionRow, Chip, TasteArtifacts, TasteProfile                                                            |
| `src/kebi/core/taste/regen.py`                       | New — prompt builder + validate_chips, loads template from config/prompts/                                          |
| `src/kebi/core/taste/debounce.py`                    | New — RegenDebouncer with cancel_all shutdown hook                                                                  |
| `src/kebi/api/main.py`                               | Wire debouncer cancel_all into FastAPI lifespan shutdown                                                            |
| `src/kebi/core/events/handlers.py`                   | Simplify — all handlers call handle_signal                                                                          |
| `src/kebi/core/ranking/service.py`                   | DELETE entire file                                                                                                  |
| `src/kebi/core/consult/service.py`                   | Remove RankingService + TasteModelService, return candidates unranked                                               |
| `src/kebi/core/consult/types.py`                     | Remove ScoredPlace                                                                                                  |
| `src/kebi/api/deps.py`                               | Remove RankingService + taste_service from consult wiring                                                           |
| Tests (7 files)                                           | Rewrite/update/delete                                                                                               |
