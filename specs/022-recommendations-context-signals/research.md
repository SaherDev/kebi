# Research: 022-recommendations-context-signals

**Date**: 2026-04-17

## Decision 1: Table rename strategy (consult_logs → recommendations)

**Decision**: Use `ALTER TABLE consult_logs RENAME TO recommendations` in a single Alembic migration. Also rename the index `ix_consult_logs_user_id` → `ix_recommendations_user_id`.

**Rationale**: PostgreSQL `ALTER TABLE RENAME` is a metadata-only operation — instant, no table rewrite, no downtime. The existing JSONB column stays as `response` (not renamed to `response_json`) to avoid breaking the ORM mapping and any downstream queries. The spec's FR-001 mentioned `response_json` but the actual column is `response` — keeping the existing name avoids unnecessary churn.

**Alternatives considered**:
- Create new table + migrate data: unnecessary overhead for a simple rename
- Keep both tables: creates confusion, dual write complexity

## Decision 2: ORM and repository rename scope

**Decision**: Rename throughout:
- `ConsultLog` → `Recommendation` (ORM model, `__tablename__ = "recommendations"`)
- `ConsultLogRepository` → `RecommendationRepository` (Protocol + SQLAlchemy impl + Null impl)
- `consult_log_repo` → `recommendation_repo` (deps.py, ConsultService constructor)
- Update all imports in `deps.py`, `service.py`, `models.py`

**Rationale**: Keeping old names after a table rename creates a naming mismatch that confuses future contributors. A clean rename across all layers keeps the codebase consistent.

## Decision 3: recommendation_id return path

**Decision**: `_persist_consult_log()` (renamed to `_persist_recommendation()`) returns `str | None` — the stringified UUID on success, `None` on failure. `ConsultResponse` has `recommendation_id: str | None = None`. The service sets it after the write succeeds.

**Rationale**: The existing pattern catches all exceptions and logs without raising. Returning `None` on failure keeps this pattern intact while making the ID available to the response. Database generates the UUID via `default=uuid4` on the ORM model — the service reads `recommendation.id` after `session.commit()`.

## Decision 4: Signal route replaces feedback route

**Decision**: Delete `src/kebi/api/routes/feedback.py` and `src/kebi/api/schemas/feedback.py`. Create `src/kebi/api/routes/signal.py` and `src/kebi/api/schemas/signal.py`. The new route uses `Literal["recommendation_accepted", "recommendation_rejected"]` for `signal_type` (Pydantic discriminated union via Literal).

**Rationale**: The existing feedback route does nearly the same thing but lacks recommendation_id validation. Replacing it avoids two endpoints doing overlapping work. The product repo must update its client to call `/v1/signal` instead of `/v1/feedback`.

**Alternatives considered**:
- Refactor feedback route in-place: route path changes anyway (`/feedback` → `/signal`), so a new file is cleaner
- Coexist: creates confusion, two endpoints for same purpose

## Decision 5: UserContext service location

**Decision**: No new service class. The user context route handler calls `TasteModelService.get_taste_profile(user_id)` directly via dependency injection. Assembling the response from `TasteProfile` fields is trivial mapping, not business logic.

**Rationale**: Creating a `UserContextService` wrapper around a single `get_taste_profile()` call would be over-abstraction. The route handler remains a facade — one service call, result mapping, return. This follows the existing pattern where routes call domain services directly.

## Decision 6: RecommendationRepository.exists() for signal validation

**Decision**: Add `async def exists(self, recommendation_id: str) -> bool` to `RecommendationRepository` protocol. The signal route calls this before dispatching. Returns 404 if `False`.

**Rationale**: The signal route needs to validate `recommendation_id` exists before accepting. A dedicated `exists()` method is more efficient than `get_by_id()` (no need to deserialize the full JSONB response). Uses `SELECT 1 FROM recommendations WHERE id = :id LIMIT 1`.

## Decision 7: ADR and constitution updates

**Decision**: Add ADR-060 superseding ADR-053 to document the `consult_logs` → `recommendations` rename and the two new endpoints. Update constitution sections VI and VIII.

**Rationale**: ADR-053 specifically names `consult_logs`. The rename changes this binding constraint. Constitution sections VI and VIII list owned tables and endpoints — both need updating to reflect the new state.
