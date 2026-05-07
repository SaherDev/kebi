# Phase 1 Data Model: Onboarding Signal Tier

Pydantic models and ORM columns touched by feature 023. All shapes are Pydantic (Constitution IV) — no raw dicts cross module boundaries.

---

## Chip (extended)

**Location**: `src/kebi/core/taste/schemas.py`

```python
class ChipStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class SelectionRound(str, Enum):
    ROUND_1 = "round_1"
    ROUND_2 = "round_2"


class Chip(BaseModel):
    label: str = Field(min_length=1, max_length=30)
    source_field: str
    source_value: str
    signal_count: int
    status: ChipStatus = ChipStatus.PENDING            # NEW — defaults pending for legacy rows
    selection_round: SelectionRound | None = None      # NEW — null until confirm/reject
```

**Invariants**:
- Identity: `(source_field, source_value)` uniquely identifies a chip within a user's chip array.
- `status == CONFIRMED ⇒ selection_round is not None` (enforced by merge + API validation).
- `status == PENDING ⇒ selection_round is None` (enforced by regen merge).
- `status == REJECTED ⇒ selection_round is not None` at the moment of rejection, but MAY be reset to `(PENDING, None)` by a later regen if signal count grows (Q1 Option C).

**Lifecycle transitions** (handled in `core/taste/chip_merge.py`):

```
(absent) ──regen signal crosses chip_threshold──> (PENDING, round=null)
(PENDING) ──chip_confirm status=confirmed──> (CONFIRMED, round=<supplied>)     # permanent
(PENDING) ──chip_confirm status=rejected──> (REJECTED, round=<supplied>)      # may re-surface
(REJECTED, round=r) ──regen with growing signal_count──> (PENDING, round=null)
(CONFIRMED, round=r) ──any event──> (CONFIRMED, round=r)                       # immutable
```

---

## ChipConfirmMetadata / ChipConfirmChipItem

**Location**: `src/kebi/api/schemas/signal.py`

```python
class ChipConfirmChipItem(BaseModel):
    label: str
    signal_count: int
    source_field: str
    source_value: str
    status: Literal["confirmed", "rejected"]            # pending not allowed at boundary
    selection_round: SelectionRound


class ChipConfirmMetadata(BaseModel):
    round: SelectionRound
    chips: list[ChipConfirmChipItem] = Field(min_length=1)
```

**Validation**:
- `status` must be `confirmed` or `rejected` — never `pending` (users cannot submit an undecided chip).
- `round` must match every `chips[i].selection_round` (FastAPI `model_validator` enforces).
- `chips` must be non-empty.

---

## SignalRequest (discriminated union)

**Location**: `src/kebi/api/schemas/signal.py`

```python
class RecommendationSignalRequest(BaseModel):
    signal_type: Literal["recommendation_accepted", "recommendation_rejected"]
    user_id: str
    recommendation_id: str
    place_id: str


class ChipConfirmSignalRequest(BaseModel):
    signal_type: Literal["chip_confirm"]
    user_id: str
    metadata: ChipConfirmMetadata


SignalRequest = Annotated[
    RecommendationSignalRequest | ChipConfirmSignalRequest,
    Field(discriminator="signal_type"),
]
```

Pydantic 2 produces a 422 for any request whose `signal_type` doesn't match one of the variants, with the discriminator field called out in the error path.

---

## SignalTier (Literal)

**Location**: `src/kebi/core/taste/tier.py`

```python
SignalTier = Literal["cold", "warming", "chip_selection", "active"]


def derive_signal_tier(
    signal_count: int,
    chips: list[Chip],
    stages: ChipSelectionStagesConfig,
) -> SignalTier:
    ...
```

**Derivation rules** (spec FR-003):
1. `signal_count == 0` → `cold`
2. `signal_count < stages.round_1` → `warming`
3. `signal_count >= any_stage_threshold AND any chip has status == PENDING` → `chip_selection`
4. `signal_count >= any_stage_threshold AND no pending chips` → `active`

`any_stage_threshold` = highest stage in `stages` whose threshold ≤ `signal_count`. This is a pure function — trivially parameterized over `stages` for table-driven tests.

---

## ChipConfirmed event

**Location**: `src/kebi/core/events/events.py`

```python
class ChipConfirmed(DomainEvent):
    event_type: str = "chip_confirmed"
    # Handler re-reads taste_model from DB — the chip statuses have already been
    # merged by SignalService before dispatch, so the event carries no chip payload.
```

Carrying only `user_id` (from `DomainEvent`) keeps the event small and lets the handler read fresh state, avoiding stale-payload bugs if more signals arrive between dispatch and handler execution.

---

## Interaction (ORM + repository)

**Location**: `src/kebi/db/models.py`

```python
class InteractionType(PyEnum):
    SAVE = "save"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ONBOARDING_CONFIRM = "onboarding_confirm"
    ONBOARDING_DISMISS = "onboarding_dismiss"
    CHIP_CONFIRM = "chip_confirm"        # NEW


class Interaction(Base):
    __tablename__ = "interactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[InteractionType] = mapped_column(Enum(InteractionType, ...))
    place_id: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_: Mapped[dict | None] = mapped_column(            # NEW — JSONB
        "metadata", JSONB, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(...)
```

Attribute name is `metadata_` because `Base.metadata` is reserved by SQLAlchemy; the DB column is still `metadata` via the positional `Column(...)` name. Repository method gains a `metadata: dict | None = None` keyword parameter.

---

## ConsultResponse — unchanged

**Location**: `src/kebi/api/schemas/consult.py`

Feature 023 makes **no** schema change to `ConsultResponse`. The product
repo gates on `signal_tier` from `GET /v1/user/context` and does not call
`/v1/consult` at cold or chip_selection tiers. `ChatService` and route
handlers are therefore untouched by this feature — the only behavioral
change inside `/v1/consult` is a config-driven warming-tier candidate-count
mix (see `contracts/consult.md`), which does not alter the response shape.

Previous drafts proposed a `message_type` discriminator + `pending_chips`
payload on `ConsultResponse`; that design was dropped (research.md
Decision 8) to avoid duplicating tier-gating responsibility across the AI
repo and the product repo.

---

## UserContextResponse (extended)

**Location**: `src/kebi/api/schemas/user_context.py`

```python
class ChipResponse(BaseModel):
    label: str
    source_field: str
    source_value: str
    signal_count: int
    status: ChipStatus                                  # NEW
    selection_round: SelectionRound | None = None       # NEW


class UserContextResponse(BaseModel):
    user_id: str                                        # NEW (previously implicit via query)
    saved_places_count: int
    signal_tier: SignalTier                             # NEW
    chips: list[ChipResponse] = Field(default_factory=list)
```

`user_id` is echoed back so clients can correlate responses when fanning out requests.

---

## TasteModelConfig (extended)

**Location**: `src/kebi/core/config.py`

```python
class ChipSelectionStagesConfig(BaseModel):
    round_1: int = 5
    round_2: int = 50


class WarmingBlendConfig(BaseModel):
    discovered: float = 0.8
    saved: float = 0.2

    @model_validator(mode="after")
    def _sum_to_one(self) -> "WarmingBlendConfig":
        if abs((self.discovered + self.saved) - 1.0) > 1e-6:
            raise ValueError("warming_blend weights must sum to 1.0")
        return self


class TasteModelConfig(BaseModel):
    debounce_window_seconds: int = 30
    regen: TasteRegenConfig = TasteRegenConfig()
    chip_threshold: int = 2                             # NEW
    chip_max_count: int = 8                             # NEW
    chip_selection_stages: ChipSelectionStagesConfig = ChipSelectionStagesConfig()
    warming_blend: WarmingBlendConfig = WarmingBlendConfig()
```

---

## taste_regen prompt registry

**Location**: `config/app.yaml` under `prompts:`

```yaml
prompts:
  taste_regen:
    name: taste_regen
    file: taste_regen.txt
  taste_regen_with_chips:                     # NEW
    name: taste_regen_with_chips
    file: taste_regen_with_chips.txt
```

Selection at runtime: `TasteModelService._run_regen` picks the prompt based on whether any chip in the stored array has `status ∈ {confirmed, rejected}`.

---

## Out-of-scope data shapes

- `places`, `embeddings`, `recommendations` — untouched.
- `user_memories` — untouched.
- `places.attributes` — untouched; `source_field` strings in chips reference attribute paths but don't change attribute schema.
