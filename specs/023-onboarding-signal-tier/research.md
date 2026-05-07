# Phase 0 Research: Onboarding Signal Tier

All Technical Context items are resolved from code inspection of the live repo — no NEEDS CLARIFICATION markers remain. Spec clarifications (Q1–Q5) already closed the open product decisions. This file records the technical decisions that shape Phase 1.

---

## Decision 1: Where the signal tier is computed

- **Decision**: Pure function `derive_signal_tier(signal_count: int, chips: list[Chip], stages: dict[str, int]) -> Literal["cold","warming","chip_selection","active"]` in a new module `src/kebi/core/taste/tier.py`. Called by `GET /v1/user/context` and by `ConsultService.consult()` at request entry.
- **Rationale**: The spec (FR-002) bans persisting the tier. The agent's `ContextNode` / `AgentState` do not yet exist (ADR-058 deferred agent build). A shared pure function avoids duplicating derivation logic in two places today and remains trivially importable by a future LangGraph `ContextNode`. Pure-function + `Literal` return is unit-testable at every threshold boundary without fixtures.
- **Alternatives considered**:
  - Method on `TasteModelService` — rejected because derivation has no side-effects and no dependency on a DB session. Pure function is simpler.
  - Property on `TasteProfile` — rejected because derivation needs `chip_selection_stages` from `AppConfig`, which shouldn't be loaded inside a read model.
  - Compute inside `ContextNode` only — rejected because `/v1/user/context` also needs it; would force duplication.

## Decision 2: Tier gating lives in the product repo (revised 2026-04-18)

- **Decision**: The product repo reads `GET /v1/user/context`, sees `signal_tier`, and decides whether to call `/v1/consult` at all. At `cold` and `chip_selection`, the product repo renders its own UI directly from the user-context response and never hits `/v1/consult`. `/v1/consult` is only called at `warming` or `active`. Inside `ConsultService.consult()`, the only tier-aware behavior is applying the warming-tier candidate-count blend when `signal_tier == "warming"`; it does not short-circuit or return a discriminator for cold/chip_selection.
- **Rationale**: Having both the product repo gate on `signal_tier` AND the consult endpoint return a `message_type` envelope creates two sources of truth. One has to win. The product-repo-gates model is the simpler contract: `/v1/consult` is consult, `/v1/user/context` is the tier surface. It also matches how other endpoints in this repo behave — no discriminator envelopes, one responsibility per endpoint.
- **Alternatives considered**:
  - Envelope discriminator on `ConsultResponse` (previous draft) — rejected. Double-gating; contradicts the product-repo-gates intent.
  - Server-side 409/400 at cold/chip_selection — rejected. If the frontend is the source of truth, a well-behaved frontend never calls this path; a rogue client posting anyway just gets pipeline output and no harm done. Explicit rejection would create an unnecessary special case.
  - Put tier gating in `ChatService._dispatch` — rejected for the same reason: `ChatService` is a facade (ADR-034), and tier gating is a product-UX concern not an AI-pipeline concern.
  - Apply warming blend as ranking weights — rejected. No ranker exists today (ADR-058). Candidate-count mix is the only mechanism.

## Decision 3: Two prompt files vs one prompt with conditional sections

- **Decision**: Two separate prompt files under `config/prompts/`:
  - `taste_regen.txt` (existing, modified) — behavioral-signals-only, used when `chips` contains no confirmed or rejected items. Updated to emit `status="pending"` and `selection_round=null` in chip output.
  - `taste_regen_with_chips.txt` (new) — four-section prompt (behavioral signals, confirmed, rejected, pending). Used when any chip has `status ∈ {confirmed, rejected}`.
- **Rationale**: FR-017 and FR-018 describe two prompts with materially different structure. Separate files are clearer, independently tunable, and easier to eval. They follow the ADR-059 convention (one prompt per file, registered in `app.yaml`).
- **Alternatives considered**:
  - Single prompt with `{if_confirmed}`-style templating — rejected. Python string replacement gets brittle; the prompts read very differently.
  - Always use the four-section prompt with empty sections — rejected. The pre-chip-confirmation user has no confirmed/rejected data; the extra sections add tokens and confuse the LLM at exactly the phase where signals are weakest.

## Decision 4: Chip merge semantics during regen

- **Decision**: New pure function `merge_chips(existing: list[Chip], fresh: list[Chip]) -> list[Chip]` in `src/kebi/core/taste/chip_merge.py`. Rules per spec FR-005, FR-006, clarification Q1 (Option C — rejected may re-surface):
  - For each `existing` chip keyed by `(source_field, source_value)`:
    - If `status == "confirmed"` → preserve verbatim (ignore any fresh chip with same key).
    - If `status == "rejected"` → look up fresh chip with same key. If fresh's `signal_count > existing.signal_count`, reset to `status="pending"`, `selection_round=null`, update `signal_count` from fresh. Else preserve.
    - If `status == "pending"` → update `signal_count` from fresh chip if present.
  - For each `fresh` chip not matched in `existing` → add with `status="pending"`, `selection_round=null`.
- **Rationale**: Puts the invariant enforcement in one testable place so `TasteModelService._run_regen` doesn't sprawl. Keeps the LLM-produced chips "fresh" (what would emerge from current signal_counts) and merges deterministically. The LLM never sees or outputs `status` or `selection_round` — those are service-owned fields.
- **Alternatives considered**:
  - Let the LLM emit status — rejected. Violates grounding (validate_grounded) and risks status flips we can't trust.
  - Inline merge in `_run_regen` — rejected. Too many branches; harder to unit-test at each transition edge.

## Decision 5: ChipConfirmed event handler bypasses the debouncer

- **Decision**: New `ChipConfirmed` event. Its handler calls a new `TasteModelService.run_regen_now(user_id)` that skips the debouncer and directly invokes the regen path with the four-section prompt. Existing `handle_signal` (used by SAVE/ACCEPTED/REJECTED) continues to go through the debouncer.
- **Rationale**: Chip confirmation is a one-shot explicit user action; debouncing it by 30 seconds would delay the taste summary rewrite that the user immediately expects (SC-004). Save/accept/reject are rapid-fire streams where debouncing prevents regen storms — that motivation doesn't apply to chip_confirm.
- **Alternatives considered**:
  - Reuse debouncer for chip_confirm — rejected for the latency reason above.
  - Run rewrite synchronously before returning 202 — rejected. Violates FR-012 (fire-and-forget) and ADR-043 (handlers run as background tasks).

## Decision 6: Alembic migration scope

- **Decision**: One migration adds two changes:
  1. `ALTER TYPE interaction_type ADD VALUE 'chip_confirm'` (PostgreSQL native enum)
  2. `ALTER TABLE interactions ADD COLUMN metadata JSONB NULL`
- **Rationale**: `taste_model.chips` stays as JSONB array — no DDL change. Existing chip rows will lack `status` and `selection_round`; Pydantic defaults (`status="pending"`, `selection_round=None`) absorb that transparently at the read boundary, so no data backfill is needed. Per ADR-058's simplified interactions table and Constitution VI this repo owns the migration.
- **Alternatives considered**:
  - Separate migrations for enum and column — rejected. Two migrations for one feature is noise.
  - Data backfill of existing chips to `{status: "pending", selection_round: null}` — rejected. Pydantic defaults handle the read transparently, and existing users have few chips anyway (feature 021 shipped recently). Keeps migration zero-data-change.

## Decision 7: SignalRequest discriminated union

- **Decision**: Refactor `SignalRequest` from a flat model into a discriminated union:

  ```python
  SignalRequest = Annotated[
      RecommendationSignalRequest | ChipConfirmSignalRequest,
      Field(discriminator="signal_type"),
  ]
  ```

  `RecommendationSignalRequest` keeps existing shape. `ChipConfirmSignalRequest` carries `signal_type: Literal["chip_confirm"]`, `user_id`, and `metadata: ChipConfirmMetadata`.
- **Rationale**: FR-008 requires payload differences (chip_confirm has no `recommendation_id`/`place_id`; it has `round` + chip list). Pydantic 2 discriminators produce clean 422s for invalid combos and generate correct OpenAPI. Mirrors the `ParsedIntent` discriminated-union pattern already used in this repo.
- **Alternatives considered**:
  - One flat model with all fields optional — rejected. Weakens validation, produces ambiguous 422s.
  - Two separate endpoints (`/v1/signal/recommendation` and `/v1/signal/chip_confirm`) — rejected. Violates ADR-060 which consolidated signals under `/v1/signal`.

## Decision 8: No envelope on `ConsultResponse` (revised 2026-04-18)

- **Decision**: `ConsultResponse` is **not** extended. No `message_type` field, no `pending_chips` field. The product repo reads `signal_tier` + pending chips from `GET /v1/user/context` and decides whether to render consult UI at all.
- **Rationale**: Supersedes the earlier envelope proposal. See Decision 2 — tier gating lives in the product repo. Adding an envelope would duplicate responsibility. The `pending_chips` the UI needs already ride on the user-context response; there is no second place they should appear.
- **Alternatives considered** (all rejected per Decision 2):
  - Envelope discriminator, server-side rejection at cold/chip_selection, ChatService-level mapping.

## Decision 9: Config schema additions (revised 2026-04-18)

- **Decision**: Extend `TasteModelConfig` (`src/kebi/core/config.py`) with:

  ```python
  class WarmingBlendConfig(BaseModel):
      discovered: float = 0.8
      saved: float = 0.2

      @model_validator(mode="after")
      def _sum_to_one(self) -> "WarmingBlendConfig":
          ...

  class TasteModelConfig(BaseModel):
      debounce_window_seconds: int = 30
      regen: TasteRegenConfig = TasteRegenConfig()
      chip_threshold: int = 2
      chip_max_count: int = 8
      chip_selection_stages: dict[str, int] = Field(
          default_factory=lambda: {"round_1": 5, "round_2": 20, "round_3": 50}
      )
      warming_blend: WarmingBlendConfig = WarmingBlendConfig()
  ```

  `config/app.yaml` gains a matching `taste_model:` block with `round_1: 5`, `round_2: 20`, `round_3: 50`.
- **Rationale**: FR-004 requires config for chip_threshold, chip_max_count, chip_selection_stages, and warming_blend. `chip_selection_stages` is a `dict[str, int]` (not a named-field BaseModel) because the user requirement is that "adding a new round must work with zero code changes" — a fixed-key BaseModel would force a code edit for `round_3`, `round_4`, etc. `derive_signal_tier` iterates `stages.values()`, so stage names are opaque to the function. `warming_blend` stays a BaseModel because its keys are load-bearing (discovered vs saved) and benefit from a sum-to-one validator. Three rounds ship in the default: `round_1=5`, `round_2=20`, `round_3=50`.
- **Alternatives considered**:
  - Named-field `ChipSelectionStagesConfig(BaseModel)` with `round_1`, `round_2` — rejected after implementation review. Couples the config shape to a specific number of rounds; violates "zero code change to add round_N."
  - Top-level `onboarding` config block — rejected. Thematically these are taste-model concerns and already live alongside debounce/regen settings.
- **Tradeoff accepted**: `dict[str, int]` is weaker-typed than a BaseModel — callers must not typo stage names. Mitigated because the function consumes `.values()` and never references stage keys by name; the YAML file is the only place stage names appear.

## Decision 10: Register handler wiring in app lifespan

- **Decision**: Register `on_chip_confirmed` handler in the existing `EventHandlers` container and the event wiring layer at app startup (same file that registers `on_taste_signal` and `on_personal_facts_extracted` today).
- **Rationale**: ADR-043 requires a single handler registration site. `on_chip_confirmed` is a sibling handler to the existing ones. No change to dispatcher wiring primitives.
- **Alternatives considered**: None material.

---

## Open items for Phase 2 (/speckit.tasks)

- Ordering — migration should land before code changes are merged to `dev` so test DB and deploys stay consistent.
- Langfuse span naming for `on_chip_confirmed` handler (per ADR-025) — use `taste.chip_confirmed_regen` to keep dashboard readable.
- Draft ADR-061 after implementation settles — captures the tier derivation rules and the two-prompt strategy for the decisions log.
