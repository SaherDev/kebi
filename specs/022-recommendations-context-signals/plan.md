# Implementation Plan: Recommendations Persistence, User Context, and Signal Verification

**Branch**: `022-recommendations-context-signals` | **Date**: 2026-04-17 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/022-recommendations-context-signals/spec.md`

## Summary

Three changes: (1) Rename `consult_logs` → `recommendations` and return `recommendation_id` from ConsultService, (2) Add `GET /v1/user/context` returning taste chips + saved count, (3) Replace `POST /v1/feedback` with `POST /v1/signal` with recommendation_id validation. All follow existing patterns: facade routes, Pydantic schemas, EventDispatcher, repository protocol.

## Technical Context

**Language/Version**: Python 3.11
**Primary Dependencies**: FastAPI 0.115, Pydantic 2.10, SQLAlchemy async, Alembic, Langfuse
**Storage**: PostgreSQL (pgvector), Redis
**Testing**: pytest (asyncio_mode = "auto")
**Target Platform**: Linux server (Railway)
**Project Type**: Web service (AI engine)
**Performance Goals**: <500ms p95 for user/context, <20s for consult
**Constraints**: Recommendation write must not block consult response; signal dispatch is fire-and-forget
**Scale/Scope**: 3 files modified, 4 files created, 2 files deleted, 1 migration

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Gate | Status | Notes |
|------|--------|-------|
| I. Repo Boundary | PASS | All changes within AI repo. No UI, auth, or CRUD. |
| II. ADR Compliance | REQUIRES UPDATE | ADR-053 names `consult_logs` — needs superseding ADR-060. |
| III. Provider Abstraction | N/A | No LLM/model changes. |
| IV. Pydantic Everywhere | PASS | All new schemas are Pydantic BaseModel. |
| V. Configuration Rules | PASS | No config changes needed. |
| VI. Database Write Ownership | REQUIRES UPDATE | Constitution lists `consult_logs` — update to `recommendations`. |
| VII. Redis Ownership | N/A | No Redis changes. |
| VIII. API Contract | REQUIRES UPDATE | Two new endpoints need documenting. One deleted. |
| IX. Testing | PASS | New modules get corresponding test files. |
| X. Git & Commits | PASS | `.bru` files for new endpoints. |

**Resolution**: ADR-060 added during implementation. Constitution sections VI and VIII updated. `docs/api-contract.md` updated.

## Project Structure

### Documentation (this feature)

```text
specs/022-recommendations-context-signals/
├── plan.md              # This file
├── spec.md              # Feature specification
├── research.md          # Phase 0 research decisions
├── data-model.md        # Entity definitions
├── quickstart.md        # Dev setup and manual testing
├── contracts/
│   └── api.md           # API contract changes
├── checklists/
│   └── requirements.md  # Spec quality checklist
└── tasks.md             # Phase 2 output (/speckit.tasks)
```

### Source Code (repository root)

```text
src/kebi/
├── api/
│   ├── main.py                          # MODIFY: swap feedback_router → signal_router, add user_context_router
│   ├── routes/
│   │   ├── feedback.py                  # DELETE: replaced by signal.py
│   │   ├── signal.py                    # CREATE: POST /v1/signal
│   │   └── user_context.py             # CREATE: GET /v1/user/context
│   └── schemas/
│       ├── consult.py                   # MODIFY: add recommendation_id to ConsultResponse
│       ├── feedback.py                  # DELETE: replaced by signal.py schemas
│       ├── signal.py                    # CREATE: SignalRequest, SignalResponse
│       └── user_context.py             # CREATE: UserContextResponse, ChipResponse
├── core/
│   └── consult/
│       └── service.py                   # MODIFY: _persist_consult_log → _persist_recommendation, return ID
├── db/
│   ├── models.py                        # MODIFY: ConsultLog → Recommendation, __tablename__ = "recommendations"
│   └── repositories/
│       └── consult_log_repository.py    # MODIFY: rename to recommendation_repository.py (classes + file)

alembic/versions/
└── xxxx_rename_consult_logs_to_recommendations.py  # CREATE: ALTER TABLE RENAME

docs/
├── api-contract.md                      # MODIFY: add user/context and signal, remove feedback
└── decisions.md                         # MODIFY: add ADR-060

tests/
├── api/routes/
│   ├── test_signal.py                   # CREATE
│   └── test_user_context.py            # CREATE
└── db/repositories/
    └── test_recommendation_repository.py # CREATE (if test existed for consult_log)
```

**Structure Decision**: Follows existing `src/kebi/` layout (ADR-001/002). Each new endpoint gets its own route file and schema file per ADR-018 pattern. Repository follows existing Protocol + SQLAlchemy + Null pattern.

## Complexity Tracking

No constitution violations requiring justification. All gates pass or require documentation updates (not architectural changes).
