# Implementation Plan: Onboarding Signal Tier

**Branch**: `023-onboarding-signal-tier` | **Date**: 2026-04-18 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/023-onboarding-signal-tier/spec.md`

## Summary

Introduce a signal-maturity tier (`cold` / `warming` / `chip_selection` / `active`) exposed by `GET /v1/user/context`, and drive explicit taste confirmation via a new `chip_confirm` variant on `POST /v1/signal`. The product repo is the single gate — it reads `signal_tier` and the attached pending chips, and only calls `/v1/consult` at `warming` or `active`. Extend the existing `chips` JSONB array on `taste_model` with a status lifecycle (`pending` / `confirmed` / `rejected`) and a `selection_round`. Add `CHIP_CONFIRM` to `InteractionType`, attach structured `metadata` to interaction rows. Taste regen uses a single updated `taste_regen.txt` prompt that accepts optional `confirmed_chips` / `rejected_chips` input arrays; it emits assertive/negative sentences with `[confirmed]` / `[rejected]` annotations when those arrays are populated, and behaves exactly as before when they are empty. `GET /v1/user/context` returns `signal_tier` and the full chip array (label + status + selection_round + signal_count). Inside `/v1/consult`, the only feature-023 behavior is applying a config-driven warming-tier discovery/saved candidate-count blend; `ConsultResponse` is not extended with any envelope. Rejected chips additionally exclude matching candidates in the active tier.

## Technical Context

**Language/Version**: Python 3.11 (`pyproject.toml`, ADR-006)
**Primary Dependencies**: FastAPI, SQLAlchemy async + asyncpg, Pydantic 2, LangGraph/LangChain (future), Instructor, pgvector, Redis, Langfuse, Alembic
**Storage**: PostgreSQL on Railway — this feature touches `interactions` and `taste_model` only; no new tables. pgvector and `places` unchanged.
**Testing**: pytest with `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed). Unit + integration tests co-located under `tests/` mirroring `src/`.
**Target Platform**: Linux container (Railway), FastAPI ASGI
**Project Type**: Single project, src layout (ADR-001)
**Performance Goals**: `GET /v1/user/context` stays below existing read-only endpoint expectations (single DB round-trip, no LLM call). Chip_confirm returns 202 before rewrite completes (ADR-043 pattern). Taste rewrite LLM latency unchanged.
**Constraints**: `mypy --strict`, `ruff check`, `ruff format` must pass. No hardcoded model names (ADR-020). No raw dicts across module boundaries (ADR-017, Constitution IV). All LLM calls traced via Langfuse (ADR-025). Redis exclusive to this repo (Constitution VII).
**Scale/Scope**: 22 functional requirements, 5 user stories, 1 Alembic migration, ~10 source files touched (schemas + routes + services + handlers + config), 1 new prompt file, 1 Bruno request.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Compliance | Notes |
|---|---|---|
| I. Repo Boundary (NON-NEGOTIABLE) | PASS | Only changes to this repo's AI-owned tables (`interactions`, `taste_model`). No product-side or user-table writes. |
| II. ADR Compliance | PASS | Extends ADR-058 (signal_counts + chips), ADR-043 (domain event dispatcher), ADR-059 (config-driven prompts), ADR-060 (`/v1/signal`). No superseding ADR required. New ADR-061 drafted for the tier derivation (see Phase 0). |
| III. Provider Abstraction (NON-NEGOTIABLE) | PASS | Reuses `get_llm("taste_regen")` via provider abstraction. No new model names in code. |
| IV. Pydantic Everywhere | PASS | Extended `Chip` with `status`/`selection_round`; new `UserContext`/`ChipView`/`ChipConfirmMetadata`/`ChipConfirmed` models all Pydantic. Discriminated union on `SignalRequest`. |
| V. Configuration Rules | PASS | New fields under existing `taste_model` block in `config/app.yaml`. New prompt under `config/prompts/`. Loaded via `get_config()` / `get_prompt()`. |
| VI. Database Write Ownership | PASS | This repo writes `interactions` and `taste_model` only. |
| VII. Redis Ownership | PASS | No Redis changes. |
| VIII. API Contract | PASS | Extends existing routes (`/v1/signal` discriminated union, `/v1/user/context` adds `signal_tier` and status/selection_round on chips). `/v1/consult` response shape unchanged — warming-tier behavior is internal. No new endpoints. Bruno request added for `chip_confirm`. |
| IX. Testing | PASS | New unit tests for tier derivation, chip merge, chip_confirm handler, regen-with-chips prompt; integration tests for `/v1/signal` chip_confirm flow and `/v1/user/context` response shape. |
| X. Git & Commits | PASS | Scope tags: `feat(taste)`, `feat(api)`, `feat(db)`, `feat(config)`, `test(taste)`. Single feature branch off `dev`. |

No violations. Complexity Tracking table below remains empty.

## Project Structure

### Documentation (this feature)

```text
specs/023-onboarding-signal-tier/
├── plan.md              # This file
├── research.md          # Phase 0 — decisions & rationale
├── data-model.md        # Phase 1 — entity shapes
├── contracts/
│   ├── signal.md        # POST /v1/signal discriminated union + chip_confirm variant
│   ├── user_context.md  # GET /v1/user/context updated response shape
│   └── consult.md       # POST /v1/consult — response unchanged; warming-tier candidate blend
├── quickstart.md        # Phase 1 — dev walkthrough
├── spec.md              # Feature spec
├── checklists/
│   └── requirements.md  # Spec quality checklist (from /speckit.specify)
└── tasks.md             # Phase 2 output (/speckit.tasks — NOT created here)
```

### Source Code (repository root)

Changes are localized within the existing layout (ADR-001, ADR-002). Files marked `[M]` = modified, `[N]` = new.

```text
src/kebi/
├── api/
│   ├── routes/
│   │   ├── signal.py                    [M] accept discriminated union (chip_confirm variant)
│   │   └── user_context.py              [M] thin facade — delegates to TasteModelService.get_user_context
│   └── schemas/
│       ├── signal.py                    [M] discriminated union: RecommendationSignal | ChipConfirmSignal
│       ├── user_context.py              [M] re-export UserContext/ChipView from core.taste.schemas
│       └── consult.py                   [unchanged]
├── core/
│   ├── config.py                        [M] extend TasteModelConfig with chip fields + warming_blend
│   ├── consult/
│   │   └── service.py                   [M] apply warming-tier candidate-count blend when tier=warming; filter rejected-chip candidates in active
│   ├── chat/
│   │   └── service.py                   [unchanged]
│   ├── events/
│   │   ├── events.py                    [M] add ChipConfirmed event
│   │   └── handlers.py                  [M] add on_chip_confirmed handler (force-regen, no debounce)
│   ├── signal/
│   │   └── service.py                   [M] handle chip_confirm: write CHIP_CONFIRM row + merge chip statuses + dispatch
│   └── taste/
│       ├── chip_merge.py                [N] pure fn: merge_chip_statuses (signal handler) + merge_chips_after_regen (regen preservation)
│       ├── regen.py                     [M] build_regen_messages passes confirmed_chips/rejected_chips derived from existing chips
│       ├── schemas.py                   [M] Chip gets status + selection_round; add UserContext + ChipView + SignalTier
│       ├── service.py                   [M] add get_user_context, run_regen_now; regen merges chip statuses with preservation
│       └── tier.py                      [N] pure fn derive_signal_tier(signal_count, chips, stages, chip_threshold) → SignalTier
├── db/
│   ├── models.py                        [M] InteractionType.CHIP_CONFIRM; Interaction.metadata JSONB nullable
│   └── repositories/
│       └── taste_model_repository.py    [M] log_interaction accepts metadata; add merge_chip_statuses method

alembic/versions/
└── <hash>_chip_confirm_and_metadata.py  [N] add CHIP_CONFIRM to enum; add metadata JSONB column

config/
├── app.yaml                             [M] taste_model: chip_threshold, chip_max_count, chip_selection_stages, warming_blend
└── prompts/
    └── taste_regen.txt                  [M] accept optional confirmed_chips/rejected_chips; emit chip status; annotation rules

tests/
├── api/routes/
│   ├── test_signal.py                   [M] add chip_confirm happy path + idempotency + invalid variants
│   └── test_user_context.py             [M/N] assert signal_tier + full chip shape per tier
├── core/
│   ├── consult/
│   │   └── test_service.py              [M] warming candidate-count blend; active-tier rejected-chip filtering and confirmed reasoning step
│   ├── signal/
│   │   └── test_service.py              [M] chip_confirm dispatch, merge, event fired
│   └── taste/
│       ├── test_chip_merge.py           [N] unit tests for merge semantics (preserve confirmed, resurface rejected)
│       ├── test_regen.py                [M] prompt branching; four-section prompt structure
│       ├── test_service.py              [M] confirmed-chip preservation across regen cycles
│       └── test_tier.py                 [N] unit tests for derive_signal_tier at each threshold

kebi-config/bruno/                     [M] new request file: signal-chip-confirm.bru (Constitution X)
```

**Structure Decision**: Single-project src layout (ADR-001, ADR-002). All changes sit within existing module boundaries — taste model, events, signal service, consult service, chat service, api schemas, db. Two new pure-function modules (`taste/tier.py`, `taste/chip_merge.py`) absorb the derivation + merge logic so they are trivially unit-testable and keep services thin.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

No violations — table intentionally empty.
