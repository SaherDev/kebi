# Implementation Plan: Taste Profile & Memory Redesign

**Branch**: `021-taste-profile-memory` | **Date**: 2026-04-17 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/021-taste-profile-memory/spec.md`

## Summary

Replace the EMA-based 8-dimensional taste model with signal_counts (pure Python aggregation from interactions) + taste_profile_summary (structured JSONB list of grounded lines) + chips (structured JSONB list of UI labels). Delete RankingService and all EMA logic. Simplify the interactions table (drop gain, context columns; rename table; new enum). Single LLM call (GPT-4o-mini via provider abstraction) generates both artifacts; unified `validate_grounded()` drops any item not backed by signal_counts. Debounced regeneration prevents redundant LLM calls during batch saves.

## Technical Context

**Language/Version**: Python 3.11 (ADR-006)
**Primary Dependencies**: FastAPI 0.115, Pydantic 2.10, SQLAlchemy async, Alembic, Langfuse, OpenAI SDK (for GPT-4o-mini via provider abstraction)
**Storage**: PostgreSQL (shared instance, AI tables owned by Alembic), Redis (exclusively this repo)
**Testing**: pytest with asyncio_mode="auto"
**Target Platform**: Linux server (Railway)
**Project Type**: web-service (AI engine behind HTTP API)
**Performance Goals**: Regen completes within 5 seconds of debounce window expiry (SC-002)
**Constraints**: Debounce window 30s default; min 3 interactions before regen; single LLM call per regen
**Scale/Scope**: Single-user taste profile per user_id. Interactions table is append-only, aggregated fresh each regen.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Gate | Status | Notes |
|------|--------|-------|
| I. Repo Boundary | PASS | Feature is pure AI logic (taste model, aggregation, LLM summary). No UI, auth, or CRUD. |
| II. ADR Compliance | PASS | ADR-058 (new) supersedes RankingService. No existing ADR contradicted. |
| III. Provider Abstraction | PASS | LLM call uses `get_llm("taste_regen")` via provider layer. Model name in `config/app.yaml` only. |
| IV. Pydantic Everywhere | PASS | `SummaryLine`, `Chip`, `TasteArtifacts`, `SignalCounts`, `InteractionRow`, `TasteProfile` — all Pydantic. No raw dicts cross boundaries. |
| V. Configuration Rules | PASS | Non-secret config in `config/app.yaml` (taste_model block, taste_regen model role). Prompt template in `config/prompts/taste_regen.txt`. No secrets involved. |
| VI. Database Write Ownership | PASS | `taste_model` and `interaction_log`/`interactions` are in this repo's Alembic domain. Migration reshapes existing tables only. |
| VII. Redis Ownership | N/A | No Redis changes in this feature. |
| VIII. API Contract | PASS | No new endpoints. ConsultService response shape changes (ScoredPlace removed), but `/v1/chat` contract preserved. |
| IX. Testing | PASS | New test files mirror src structure: `tests/core/taste/test_aggregation.py`, `test_service.py`, `test_debounce.py`, `test_regen.py`, `test_validation.py`. Deleted: `tests/core/ranking/`. |
| X. Git & Commits | PASS | Feature branch from `dev`, commits follow `type(scope): description` format. |
| ADR-025 Langfuse | PASS | FR-010: every regen traced. FR-018: dropped items logged in metadata. |
| ADR-034 Facade | PASS | No route handler changes. Event handlers remain thin wrappers. |
| ADR-038 Protocol | PASS | `TasteModelRepository` remains a Protocol. |
| ADR-043 Domain Events | PASS | Event handlers simplified to call `handle_signal`. Same dispatcher pattern. |

No violations. No complexity tracking needed.

## Project Structure

### Documentation (this feature)

```text
specs/021-taste-profile-memory/
├── plan.md              # This file
├── spec.md              # Feature specification
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── contracts/           # Phase 1 output (no new endpoints — internal contracts only)
├── checklists/
│   └── requirements.md  # Spec quality checklist
└── tasks.md             # Phase 2 output (/speckit.tasks)
```

### Source Code (repository root)

```text
src/kebi/
├── api/
│   ├── main.py                          # Wire debouncer cancel_all into lifespan shutdown
│   └── deps.py                          # Remove RankingService + taste_service from consult wiring
├── core/
│   ├── config.py                        # Delete EMA/Ranking config classes, add TasteRegenConfig
│   ├── taste/
│   │   ├── __init__.py
│   │   ├── service.py                   # REWRITE: handle_signal + _run_regen + get_taste_profile
│   │   ├── schemas.py                   # NEW: InteractionRow, SummaryLine, Chip, TasteArtifacts, TasteProfile
│   │   ├── aggregation.py              # NEW: SignalCounts model + aggregate_signal_counts()
│   │   ├── regen.py                     # NEW: prompt builder + validate_grounded + format_summary_for_agent
│   │   └── debounce.py                  # NEW: RegenDebouncer with cancel_all
│   ├── ranking/
│   │   ├── __init__.py                  # Clean up exports
│   │   └── service.py                   # DELETE
│   ├── consult/
│   │   ├── service.py                   # Remove RankingService + TasteModelService deps
│   │   └── types.py                     # Remove ScoredPlace
│   └── events/
│       └── handlers.py                  # Simplify: all taste handlers → handle_signal
├── db/
│   ├── models.py                        # Replace SignalType→InteractionType, InteractionLog→Interaction, reshape TasteModel
│   └── repositories/
│       └── taste_model_repository.py    # REWRITE: Protocol + impl with InteractionRow, upsert with chips+summary
config/
├── app.yaml                             # Delete EMA + ranking blocks, add taste_regen role + regen config
└── prompts/
    └── taste_regen.txt                  # NEW: system prompt for summary + chips generation
alembic/versions/
└── XXXX_taste_profile_redesign.py       # NEW: interactions reshape + taste_model reshape
docs/
├── decisions.md                         # Add ADR-058
└── taste-model-architecture.md          # REWRITE for new system

tests/core/
├── taste/
│   ├── test_aggregation.py              # NEW: aggregate_signal_counts unit tests
│   ├── test_service.py                  # REWRITE: handle_signal + _run_regen + guards
│   ├── test_debounce.py                 # NEW: cancellation + cancel_all shutdown
│   ├── test_regen.py                    # NEW: prompt builder + signal_counts in prompt
│   └── test_validation.py              # NEW: validate_grounded for SummaryLine + Chip
├── ranking/                             # DELETE directory
└── events/
    └── test_handlers.py                 # Update: handlers call handle_signal
```

**Structure Decision**: Existing `src/kebi/core/taste/` module expanded with new files. No new top-level modules. Follows ADR-002 (hybrid directory: `core/` for domain modules).
