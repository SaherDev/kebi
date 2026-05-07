# Implementation Plan: Taste Model Audit Fixes

**Branch**: `009-taste-model-audit-fixes` | **Date**: 2026-03-31 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/009-taste-model-audit-fixes/spec.md`

## Summary

Fix seven correctness gaps in the taste model implementation: wire EMA updates for onboarding and recommendation signals; make the `interaction_count` increment atomic with ON CONFLICT semantics for concurrent new-user inserts; add `ambiance` to the Place model and enrich `_place_to_metadata()` with ambiance and time-of-day derivation; delete the now-dead `_increment_and_update_confidence` helper; strip all docstrings from `taste_model_repository.py`; verify YAML comment syntax.

## Technical Context

**Language/Version**: Python 3.11 (`>=3.11,<3.13`)
**Primary Dependencies**: FastAPI 0.115, SQLAlchemy 2.0 async, Alembic, asyncpg, Pydantic 2.10, pytest
**Storage**: PostgreSQL — modifying `places` table (add `ambiance` column), no schema change to `taste_model`
**Testing**: pytest with `asyncio_mode = "auto"` — no `@pytest.mark.asyncio` decorators needed
**Target Platform**: Linux server (Railway)
**Project Type**: Web service (FastAPI)
**Performance Goals**: No new performance requirements — all changes are in-process, no new I/O
**Constraints**: `ruff check src/` and `mypy src/` must produce zero new errors; 84 existing tests must pass
**Scale/Scope**: Single service, internal changes only — no new public API endpoints

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Gate | Status | Notes |
|------|--------|-------|
| Repo boundary respected | ✓ PASS | All changes in `src/kebi/` — no UI, auth, or product CRUD |
| ADR compliance | ✓ PASS | No new architectural decisions; existing patterns followed (repository protocol, Pydantic, Alembic) |
| Database write ownership | ✓ PASS | `places` and `taste_model` are owned by this repo; Alembic migration is correct migration tool |
| Provider abstraction | ✓ PASS | No LLM calls added; no model names hardcoded |
| Pydantic everywhere | ✓ PASS | No new API boundaries; internal dict `place_metadata` stays internal to service layer |
| No raw dicts at API boundary | ✓ PASS | No API schema changes |
| mypy strict | ✓ PASS | Must verify after ON CONFLICT insert — `postgresql.insert()` result type differs from `update()` |
| No new endpoints | ✓ PASS | Zero new routes; no `.bru` file needed |

**Post-design re-check**: All gates still pass. The `ON CONFLICT` INSERT uses `sqlalchemy.dialects.postgresql.insert` which is already an established pattern in the codebase for atomic upserts.

## Project Structure

### Documentation (this feature)

```text
specs/009-taste-model-audit-fixes/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
└── tasks.md             # Phase 2 output (/speckit.tasks — NOT created here)
```

### Source Code (repository root)

```text
src/kebi/
├── core/
│   └── taste/
│       └── service.py              # Fixes 1, 2, 5; delete _increment_and_update_confidence
├── db/
│   ├── models.py                   # Fix 4: add ambiance to Place
│   └── repositories/
│       └── taste_model_repository.py  # Fix 3, 6: ON CONFLICT upsert + strip docstrings
├── alembic/
│   └── versions/
│       └── <hash>_add_ambiance_to_places.py  # Fix 4: migration

config/
└── app.yaml                        # Fix 7: verify # comment chars

tests/
├── core/
│   └── taste/
│       └── test_service.py         # Updated for new handle_onboarding_signal behavior
└── db/
    └── repositories/
        └── test_taste_model_repository.py  # Updated for ON CONFLICT upsert
```

**Structure Decision**: Single project layout (Option 1). All changes are within the existing `src/` tree. No new top-level directories.

## Complexity Tracking

No constitution violations. All changes follow existing patterns.
