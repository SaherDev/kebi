# Tasks: Taste Model Audit Fixes

**Input**: Design documents from `/specs/009-taste-model-audit-fixes/`
**Prerequisites**: plan.md ✓, spec.md ✓, research.md ✓, data-model.md ✓, quickstart.md ✓

**Organization**: Tasks grouped by user story in priority order (P1 → P2).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to

---

## Phase 1: Setup

**Purpose**: No new project structure needed — all changes are within the existing codebase.

*No setup tasks — project already initialized.*

---

## Phase 2: Foundational (Blocking Prerequisite)

**Purpose**: Add `ambiance` to the Place SQLAlchemy model and create the Alembic migration. This must complete before User Story 2 can update `_place_to_metadata()`.

**⚠️ CRITICAL**: US2 work cannot begin until T001 and T002 are complete.

- [X] T001 Add `ambiance: Mapped[str | None] = mapped_column(String, nullable=True)` to `Place` in `src/kebi/db/models.py`
- [X] T002 Create Alembic migration `add_ambiance_to_places` with `op.add_column("places", sa.Column("ambiance", sa.String(), nullable=True))` and matching downgrade `op.drop_column`; run `poetry run alembic revision --autogenerate -m "add_ambiance_to_places"` and verify

**Checkpoint**: `Place.ambiance` attribute exists; migration runs without error against local DB.

---

## Phase 3: User Story 1 — EMA Updates Fire for All Signal Types (Priority: P1) 🎯 MVP

**Goal**: Every signal type (onboarding confirm/dismiss, recommendation accept/reject, save) triggers a real EMA update to the taste vector. No signal silently skips the EMA calculation.

**Independent Test**: After `handle_onboarding_signal(confirmed=True)` for a `price_range=low` place, `price_comfort` in `taste_model.parameters` increases above 0.5. After `confirmed=False`, it decreases.

- [X] T003 [US1] In `src/kebi/core/taste/service.py` — update `handle_onboarding_signal`: replace the `await self._increment_and_update_confidence(user_id)` call with: fetch place via `await self.place_repo.get_by_id(place_id)`, then call `await self._apply_taste_update(user_id, self._place_to_metadata(place), gain, is_positive=confirmed)`
- [X] T004 [US1] In `src/kebi/core/taste/service.py` — delete the `_increment_and_update_confidence` method entirely (FR-011: it has no callers after T003)

**Checkpoint**: `poetry run pytest tests/core/taste/` passes; `handle_onboarding_signal` moves the taste vector for both `confirmed=True` and `confirmed=False`.

---

## Phase 4: User Story 4 — Interaction Count Increments Without Race Condition (Priority: P1)

**Goal**: `upsert()` in `SQLAlchemyTasteModelRepository` increments `interaction_count` atomically in SQL with no Python-side read. Concurrent first-time inserts for the same user never raise a constraint violation.

**Independent Test**: Inspect the generated SQL — the UPDATE path must use `interaction_count = interaction_count + 1` with no prior SELECT. A brand-new user call produces `interaction_count=1`.

- [X] T005 [US4] In `src/kebi/db/repositories/taste_model_repository.py` — replace the INSERT fallback path: change from `TasteModel(...)` inserted directly to a PostgreSQL `insert(TasteModel).values(...).on_conflict_do_update(index_elements=["user_id"], set_=dict(interaction_count=TasteModel.interaction_count + 1, confidence=..., parameters=...))`. Add `from sqlalchemy.dialects.postgresql import insert as pg_insert`. The UPDATE path (existing row) already uses `update(TasteModel)...` — keep it; only change the INSERT fallback to use `pg_insert` with `on_conflict_do_update`.

**Checkpoint**: `poetry run pytest tests/db/repositories/` passes; new-user upsert produces `interaction_count=1`; second call produces `interaction_count=2`; no `IntegrityError` on concurrent first inserts.

---

## Phase 5: User Story 2 — Ambiance Dimension Observes Real Place Data (Priority: P2)

**Goal**: `_place_to_metadata()` returns the `ambiance` field when set on the Place record, enabling `ambiance_preference` to receive a real observation value instead of always defaulting to 0.5.

**Independent Test**: Save a place with `ambiance="upscale"` → `ambiance_preference` increases. Place with `ambiance=None` → `ambiance_preference` unchanged.

**Depends on**: T001, T002 (Place.ambiance must exist in model and DB)

- [X] T006 [US2] In `src/kebi/core/taste/service.py` — update `_place_to_metadata()`: add `ambiance` to the returned dict when `place.ambiance` is not None. Result: `{"price_range": place.price_range, "ambiance": place.ambiance}` (price_range already there; omit keys whose values are None)

**Checkpoint**: `poetry run pytest tests/core/taste/` passes; a place with `ambiance="casual"` causes `ambiance_preference` to decrease; `ambiance=None` causes no change.

---

## Phase 6: User Story 3 — Time-of-Day Preference Observes Save Time (Priority: P2)

**Goal**: `_place_to_metadata()` derives `time_of_day` from `place.created_at` UTC hour using four buckets (breakfast/lunch/dinner/late_night), giving `time_of_day_preference` a live signal at zero extra data cost.

**Independent Test**: A place with `created_at` at hour 17 (UTC) produces `time_of_day="dinner"` in metadata, and `time_of_day_preference` moves toward 0.66.

**Buckets**: breakfast=5–10, lunch=11–14, dinner=15–20, late_night=21–4 (wraps midnight).

- [X] T007 [US3] In `src/kebi/core/taste/service.py` — update `_place_to_metadata()`: derive `time_of_day` from `place.created_at.hour` using bucket logic and include it in the returned dict. Final method returns all three available keys: `price_range`, `ambiance`, `time_of_day` (each only if available/derivable).

**Checkpoint**: `poetry run pytest tests/core/taste/` passes; hours 5/11/15/21 produce breakfast/lunch/dinner/late_night respectively; `time_of_day_preference` moves in the correct direction.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Docstring removal, YAML comment verification, and full suite validation.

- [X] T008 [P] Remove all docstrings and inline comments from `src/kebi/db/repositories/taste_model_repository.py` (FR-008): delete module-level docstring, class docstrings, all method docstrings, and all `#` inline comments within method bodies
- [X] T009 [P] Verify `config/app.yaml` — search for `;` comment chars under `taste_model.observations`; replace any found with `#` (FR-009). Run `grep ";" config/app.yaml` to confirm zero matches.
- [X] T010 Run `poetry run pytest` — all 84 tests must pass
- [X] T011 [P] Run `poetry run ruff check src/` — zero errors
- [X] T012 [P] Run `poetry run mypy src/` — zero new errors

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 2 (Foundational)**: No dependencies — start immediately
- **Phase 3 (US1)**: Independent of Phase 2 — can run in parallel with Phase 2
- **Phase 4 (US4)**: Independent of Phases 2 and 3 — can run in parallel with both
- **Phase 5 (US2)**: Depends on Phase 2 completion (Place.ambiance must exist)
- **Phase 6 (US3)**: Independent of Phase 2 — but edits same file as Phase 5, so run after Phase 5
- **Phase 7 (Polish)**: T008 after Phase 4; T009 any time; T010–T012 after all phases complete

### Parallel Opportunities

```
Phase 2 ──────────────────────────────────────────► Phase 5 ► Phase 6 ►┐
Phase 3 (T003, T004) ─────────────────────────────────────────────────►│► Phase 7
Phase 4 (T005) ───────────────────────────────────────────────────────►│
T008 (docstrings, after T005) ────────────────────────────────────────►┘
T009 (YAML, anytime) ─────────────────────────────────────────────────►┘
```

Phases 2, 3, and 4 touch different files and can run concurrently:
- Phase 2: `db/models.py` + new migration file
- Phase 3: `core/taste/service.py`
- Phase 4: `db/repositories/taste_model_repository.py`

---

## Implementation Strategy

### MVP First (User Stories 1 and 4 — both P1)

1. Complete Phase 2: Add ambiance to Place model
2. Complete Phase 3 (US1): Wire EMA for onboarding signals
3. Complete Phase 4 (US4): Atomic SQL upsert with ON CONFLICT
4. **STOP and VALIDATE**: All 84 tests pass; taste vector moves on onboarding signals
5. Proceed to P2 stories if validation passes

### Incremental Delivery

1. Phase 2 → Phase 3 → Phase 4 → validate P1 complete
2. Phase 5 (US2): ambiance live → validate ambiance_preference moves
3. Phase 6 (US3): time_of_day live → validate time_of_day_preference moves
4. Phase 7: clean up docstrings, verify YAML, full suite pass

---

## Notes

- T003 and T005 touch different files — safe to parallelize
- T006 and T007 both modify `_place_to_metadata()` — run sequentially (T006 then T007) to avoid conflicts
- T008 (docstring removal) modifies `taste_model_repository.py` — run after T005 to avoid merging two edits to the same file
- No new tests requested in spec — verification is via existing test suite (84 tests) plus manual spot checks from quickstart.md
- `asyncio_mode = "auto"` in pytest config — no `@pytest.mark.asyncio` decorators needed on new test cases
