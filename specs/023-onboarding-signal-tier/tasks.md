---
description: "Task list for feature 023 — onboarding signal tier"
---

# Tasks: Onboarding Signal Tier

**Input**: Design documents from `/specs/023-onboarding-signal-tier/`
**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md`

**Tests**: Included — Success Criteria SC-005 / SC-007 / SC-008 and Constitution IX both require testable verification. Tests ship alongside implementation per repo convention.

**Organization**: Tasks are grouped by user story so each story can ship, deploy, and be validated independently.

**Gating model** (clarification Q5, revised 2026-04-18): the **product repo** gates on `signal_tier` from `GET /v1/user/context`. At `cold` and `chip_selection` it renders its own UI and never calls `/v1/consult`. `ConsultResponse` is **not** extended with an envelope discriminator. This is reflected across every task below — there are no "cold-tier short-circuit" or "chip_selection envelope" tasks on the consult path.

**Completed tasks** from the prior iteration in this feature branch are marked `[x]`; do not redo them. They remain in the list for traceability.

## Format: `[ID] [P?] [Story?] Description with file path`

- **[P]**: Parallelizable (different files, no dependencies on incomplete tasks)
- **[Story]**: `US1`–`US5` from `spec.md`

## Path Conventions

Single project, src layout (ADR-001). Code under `src/kebi/`, tests under `tests/` mirroring structure, Alembic under `alembic/versions/`, config under `config/`, Bruno under `kebi-config/bruno/`.

---

## Phase 1: Setup

Skipped — repo is established (Poetry, Alembic, Ruff, mypy, pytest already configured).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Schema, config, and shared utilities that every user story depends on.

**⚠️ CRITICAL**: Migration + `Interaction.metadata` + `InteractionType.CHIP_CONFIRM` are prerequisites for US3 and US4. Tier derivation + chip schema + config are prerequisites for US1/US2/US5.

- [x] T001 Extend `TasteModelConfig` in `src/kebi/core/config.py` with `chip_threshold: int = 2`, `chip_max_count: int = 8`, and `chip_selection_stages: dict[str, int]` (default_factory `{"round_1": 5, "round_2": 20, "round_3": 50}`). Import `Field` where needed.
- [x] T002 Update `config/app.yaml` under `taste_model:` with `chip_threshold: 2`, `chip_max_count: 8`, and the three-round `chip_selection_stages` block (`round_1: 5`, `round_2: 20`, `round_3: 50`).
- [x] T003 Extend `Chip` Pydantic model in `src/kebi/core/taste/schemas.py` with `status: ChipStatus = PENDING` and `selection_round: str | None = None`. Add the `ChipStatus` enum (`PENDING`, `CONFIRMED`, `REJECTED`).
- [x] T004 Add `SignalTier` Literal type and `UserContext` / `ChipView` response models to `src/kebi/core/taste/schemas.py`. Extend `TasteProfile` with `generated_from_log_count: int = 0`.
- [x] T005 Create `src/kebi/core/taste/tier.py` with pure function `derive_signal_tier(signal_count: int, chips: list[Chip], stages: dict[str, int], chip_threshold: int) -> SignalTier`. Iterate `stages.values()` — no stage names hardcoded.
- [x] T006 [P] Add unit tests for tier derivation in `tests/core/taste/test_tier.py` covering cold / warming / chip_selection / active across 2-round and 3-round configs, arbitrary stage names, and dict insertion-order independence.
- [x] T007 Add `InteractionType.CHIP_CONFIRM = "chip_confirm"` to the enum in `src/kebi/db/models.py`.
- [x] T008 Add nullable `metadata` JSONB column to the `Interaction` ORM model in `src/kebi/db/models.py`. Use attribute name `metadata_` mapped to DB column `"metadata"` (SQLAlchemy reserves `Base.metadata`).
- [x] T009 Create Alembic migration `alembic/versions/a7c3d2e9f4b1_chip_confirm_and_interaction_metadata.py`: `ALTER TYPE interactiontype ADD VALUE IF NOT EXISTS 'chip_confirm'` plus `ALTER TABLE interactions ADD COLUMN metadata JSONB NULL`. Downgrade drops only the column; docstring notes enum values cannot cleanly roll back in Postgres.
- [x] T010 Update `SQLAlchemyTasteModelRepository.log_interaction` in `src/kebi/db/repositories/taste_model_repository.py` to accept `metadata: dict | None = None` kwarg and persist it. Add a `merge_chip_statuses(user_id: str, updated_chips: list[dict]) -> None` method that replaces the `taste_model.chips` JSONB array in a single transaction.
- [x] T011 [P] Add repository tests in `tests/db/repositories/test_taste_model_repository.py` covering `log_interaction(metadata=...)` round-trip and `merge_chip_statuses` transactional replace.

**Checkpoint**: Foundation ready — user stories can start.

---

## Phase 3: User Story 1 — Cold tier reported on user context (Priority: P1) 🎯 MVP

**Goal**: `GET /v1/user/context` for a brand-new user returns `signal_tier="cold"`, `saved_places_count=0`, `chips=[]`. The product repo uses this to render onboarding UI; `/v1/consult` is never called for cold users.

**Independent Test**: Seed a user with zero interactions. `GET /v1/user/context?user_id=<id>` returns the cold response shape. No LLM call fires.

### Tests for User Story 1

- [x] T012 [P] [US1] Add `tests/api/routes/test_user_context.py::test_cold_user_returns_cold_tier` asserting the full response: `user_id` echoed, `saved_places_count==0`, `signal_tier=="cold"`, `chips==[]`. Plus warming, chip_selection (pending chip shape), active (confirmed+rejected preservation), and 422 on missing user_id.

### Implementation for User Story 1

- [x] T013 [US1] Add `TasteModelService.get_user_context(user_id) -> UserContext` in `src/kebi/core/taste/service.py`. Single DB read + `derive_signal_tier` call; handles no-row (cold) path.
- [x] T014 [US1] Slim `GET /v1/user/context` route in `src/kebi/api/routes/user_context.py` to one-line delegation: `return await taste_service.get_user_context(user_id)`.
- [x] T015 [US1] Re-export `UserContext` / `ChipView` from `src/kebi/api/schemas/user_context.py` as `UserContextResponse` / `ChipResponse` for backward-compat.

**Checkpoint**: MVP shipped — product repo can start gating on `signal_tier`.

---

## Phase 4: User Story 2 — Warming-tier discovery/saved candidate blend (Priority: P1)

**Goal**: When the product repo calls `/v1/consult` for a warming-tier user, the pipeline returns a candidate mix respecting the config-driven 80/20 discovered:saved split, and adds a `warming_blend` reasoning step.

**Independent Test**: Seed a user with 3 saves (below `round_1=5`), POST `/v1/consult`. With a total cap of 10, the returned results split 8 discovered / 2 saved. `reasoning_steps` includes `{step: "warming_blend", summary: "discovered=8, saved=2"}`.

### Tests for User Story 2

- [x] T016 [P] [US2] Add `tests/core/consult/test_service.py::test_warming_tier_applies_candidate_blend` stubbing both source paths and asserting the sliced final `results` respect the ratio and the `warming_blend` reasoning step is present. Also asserts active tier does not add that step.

### Implementation for User Story 2

- [x] T017 [US2] Add `WarmingBlendConfig(discovered: float = 0.8, saved: float = 0.2)` with a `model_validator(mode="after")` requiring the two fields sum to `1.0` (tolerance `1e-6`) to `src/kebi/core/config.py`. Attach as `TasteModelConfig.warming_blend`.
- [x] T018 [US2] Add matching `warming_blend:` block to `config/app.yaml` (`discovered: 0.8`, `saved: 0.2`).
- [x] T019 [US2] In `ConsultService.consult` (`src/kebi/core/consult/service.py`), derive `signal_tier` inline via `derive_signal_tier` (reusing the already-loaded `taste_profile`, no extra DB round-trip). When tier is `warming`: compute `saved_cap = round(total_cap * warming_blend.saved)`, `discovered_cap = total_cap - saved_cap`, slice the deduped pool by source, and append `ReasoningStep(step="warming_blend", summary=f"discovered={d}, saved={s}")`.

**Checkpoint**: Warming users see the config-driven candidate mix.

---

## Phase 5: User Story 3 — Chip confirm / reject flow (Priority: P1)

**Goal**: When the user crosses the round_1 threshold, `GET /v1/user/context` reports `chip_selection` with pending chips attached. The user submits `POST /v1/signal` with `signal_type=chip_confirm`; chip statuses merge correctly; tier advances to `active` once every pending chip is resolved. The product repo stops calling `/v1/consult` while the user is in chip_selection.

**Independent Test**: Seed a user with 5+ interactions and pending chips. `GET /v1/user/context` reports `signal_tier=chip_selection` with pending chips. POST the chip_confirm payload from `contracts/signal.md`. Re-GET `/v1/user/context` and verify every submitted chip's status is updated, selection_round recorded, tier becomes `active`.

### Tests for User Story 3

- [x] T020 [P] [US3] Add `tests/core/taste/test_chip_merge.py` covering merge_chip_statuses (confirmed preserved, pending/rejected overwritten, unknown submissions ignored, preserved if not in submission) AND merge_chips_after_regen (confirmed preserved even when missing from fresh, rejected resurfaces when signal grows, rejected stays when signal doesn't grow, pending signal_count updated, new fresh chips added as pending).
- [x] T021 [P] [US3] Add `tests/core/signal/test_service.py` covering happy-path interaction row write, chip status merge + persist, ChipConfirmed dispatch, unknown-chip no-op, cold-profile skip-persist, missing-metadata ValueError.
- [x] T022 [P] [US3] Add `tests/api/routes/test_signal.py` covering 202 on happy path, 422 round mismatch, 422 empty chips, 422 invalid status, 422 unknown discriminator, recommendation_accepted still routes.
- [x] T023 [P] [US3] Extend `tests/api/routes/test_user_context.py` with chip_selection and active cases (full ChipView shape at each tier).

### Implementation for User Story 3

- [x] T024 [P] [US3] Add `ChipConfirmed` domain event in `src/kebi/core/events/events.py` carrying only `user_id`.
- [x] T025 [P] [US3] Create `src/kebi/core/taste/chip_merge.py` with `merge_chip_statuses` AND `merge_chips_after_regen`.
- [x] T026 [US3] Add `ChipConfirmChipItem` and `ChipConfirmMetadata` Pydantic models to `src/kebi/api/schemas/signal.py` with round/selection_round consistency validator.
- [x] T027 [US3] Refactor `SignalRequest` in `src/kebi/api/schemas/signal.py` into a discriminated union.
- [x] T028 [US3] Extend `SignalService.handle_signal` in `src/kebi/core/signal/service.py` with chip_confirm branch. `SignalService` constructor now takes `taste_service` so it can read/merge chips and reuse the repo. `deps.get_signal_service` updated accordingly.
- [x] T029 [US3] Update `POST /v1/signal` handler to accept the discriminated union via `Body(..., discriminator="signal_type")`. Dispatches to service with variant-specific load-bearing fields.
- [x] T030 [US3] Add Bruno request file at `kebi-config/bruno/ai-service/signal-chip-confirm.bru`.

**Checkpoint**: Users in chip_selection get pending chips via `/v1/user/context`, submit chip_confirm, and advance to active.

---

## Phase 6: User Story 4 — Taste profile summary reflects chip status (Priority: P2)

**Goal**: After a chip_confirm lands, `taste_profile_summary` sentences reflect the user's explicit preferences — assertive for confirmed, explicit-negative for rejected, probabilistic with signal counts for pending — each with the correct annotation.

**Independent Test**: Seed a user with one confirmed chip, one rejected chip, and one pending chip. Trigger the chip_confirmed handler. Read back `taste_model.taste_profile_summary`; verify the three sentence types with `[confirmed]` / `[rejected]` / `[N signals]` annotations.

### Tests for User Story 4

- [x] T031 [P] [US4] Add `tests/core/taste/test_regen_prompt.py` (7 tests): empty chip input omits keys, pending-only omits keys, confirmed serialized, rejected serialized, mixed serialized, system prompt contains chip-status rules.
- [x] T032 [P] [US4] Add `TestOnChipConfirmed` class to `tests/core/events/test_handlers.py` (3 tests): calls `run_regen_now` once, ignores non-matching events, swallows exceptions per ADR-043.
- [x] T033 [P] [US4] Add `tests/core/taste/test_service.py::test_regen_preserves_confirmed_chips_and_resurfaces_rejected` AND `::test_run_regen_now_bypasses_stale_guard`.

### Implementation for User Story 4

- [x] T034 [US4] Extend `config/prompts/taste_regen.txt` with chip status rules and input shape.
- [x] T035 [US4] Update `build_regen_messages` in `src/kebi/core/taste/regen.py` to accept `existing_chips: list[Chip]` and derive confirmed/rejected sublists. Omits keys when both empty (baseline-identical JSON for pre-chip-confirmation users).
- [x] T036 [US4] Update `TasteModelService._run_regen`: read chips, pass to prompt, pipe LLM chips through `merge_chips_after_regen`. Preserves confirmed verbatim, resurfaces rejected when signal grew.
- [x] T037 [US4] Add `TasteModelService.run_regen_now(user_id)` bypassing the debouncer + stale guard + min-signals guard (passes `force=True` to `_run_regen`).
- [x] T038 [US4] Add `EventHandlers.on_chip_confirmed` that calls `run_regen_now`. Logs + Langfuse-traces failures per ADR-043.
- [x] T039 [US4] Register `on_chip_confirmed` under event type `"chip_confirmed"` in `src/kebi/api/deps.py`.

**Checkpoint**: Taste summaries reflect explicit user preferences with the right confidence level.

---

## Phase 7: User Story 5 — Active-tier personalization (Priority: P2)

**Goal**: When the product repo calls `/v1/consult` for an active-tier user, confirmed chips are surfaced via `reasoning_steps` (agent-consumable when the agent is built — ADR-058) and rejected chips exclude matching candidates before the saved-first ordering step.

**Independent Test**: Seed an active user with `cuisine=ramen` confirmed and `vibe=casual` rejected. Feed a candidate pool containing a ramen place, a non-ramen place, and a casual place. The rejected-matching candidate is absent from the response; the ramen place appears; `reasoning_steps` lists an `active_confirmed_signals` entry.

### Tests for User Story 5

- [x] T040 [P] [US5] Add `test_active_tier_excludes_rejected_chip_candidates` asserting the rejected-matching candidate is filtered before saved-first ordering and an `active_rejected_filter` reasoning step is present.
- [x] T041 [P] [US5] Add `test_active_tier_surfaces_confirmed_chips_in_reasoning_steps` asserting the `active_confirmed_signals` step entry appears.

### Implementation for User Story 5

- [x] T042 [US5] In `ConsultService.consult`, when `signal_tier == "active"`: append `ReasoningStep(step="active_confirmed_signals", summary=<comma-joined confirmed chip labels>)` and `ReasoningStep(step="active_rejected_filter", summary="Filtered N/M candidates matching rejected chips")`.
- [x] T043 [US5] Add `_place_matches_chip(place: PlaceObject, chip: Chip) -> bool` helper in `src/kebi/core/consult/service.py`. Walks dotted paths for `source`, `subcategory.<place_type>`, and `attributes.*` (including `attributes.location_context.*`). Rejected-chip filter runs before the total_cap slice.

**Checkpoint**: Active consults respect explicit user preferences.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [x] T044 [P] Ran `poetry run alembic upgrade head` — revision `a7c3d2e9f4b1` applied cleanly. `interactions.metadata` JSONB column present; `interactiontype` enum includes `chip_confirm`.
- [ ] T045 [P] Quickstart walkthrough end-to-end against the local stack — left as manual validation (code paths are covered by the 491-test pytest suite).
- [x] T046 Wrote ADR-061 in `docs/decisions.md` — config-driven signal-tier derivation, product-repo-gated routing, chip status lifecycle.
- [x] T047 [P] Updated `CLAUDE.md` Recent Changes with a sharper 023 summary (93 lines total, under 150 limit).
- [x] T048 [P] Updated `docs/api-contract.md` — `/v1/signal` chip_confirm variant + server-side handling; `/v1/user/context` full response shape with `signal_tier` + `ChipView` table; `signal_tier` optional field on `/v1/chat`.
- [x] T049 [P] Updated `docs/architecture.md` — added Signal Tier section, revised Taste Model section to reflect chip lifecycle.
- [x] T050 Ran `poetry run ruff check src/ tests/` — clean. Ran `poetry run ruff format` — 24 files reformatted.
- [x] T051 Ran `poetry run mypy src/` — only pre-existing errors remain (lambda in taste/service.py:58, to_tier1 in consult/service.py:380, one type-ignore in deps.py:164 that predates 023).
- [x] T052 Ran full `poetry run pytest` — 491/491 pass.
- [x] T053 Updated auto-memory `project_built_state.md` with the new tier + chip-status file map, migration list, and a "Gone" section listing deleted paths.

### Option B wiring (done post-plan)

- [x] T054 Added `signal_tier: SignalTierHint | None = None` to `ChatRequest` (`src/kebi/api/schemas/chat.py`) and `ConsultRequest` (`src/kebi/api/schemas/consult.py`).
- [x] T055 Updated `ChatService._dispatch` to forward `request.signal_tier or "active"` to `ConsultService.consult`.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: Skipped.
- **Phase 2 (Foundational)**: Blocks every US. Within Phase 2: T001–T006 (schema/config/tier/tests) are independent of T007–T011 (DB) and can run in parallel. T009 depends on T007+T008. T010 depends on T008+T009. T011 depends on T010.
- **Phase 3 (US1, P1)**: Depends only on Phase 2.
- **Phase 4 (US2, P1)**: Depends only on Phase 2. Independent of US1/US3.
- **Phase 5 (US3, P1)**: Depends only on Phase 2. Uses `merge_chip_statuses` from T010.
- **Phase 6 (US4, P2)**: Depends on US3 (needs `ChipConfirmed` + chip_confirm wiring).
- **Phase 7 (US5, P2)**: Depends on US3 (needs real confirmed/rejected chip data). Independent of US4.
- **Phase 8 (Polish)**: Depends on all user stories landing.

### User Story Dependencies

- **US1**: Phase 2 only.
- **US2**: Phase 2 only.
- **US3**: Phase 2 only.
- **US4**: US3.
- **US5**: US3.

### Within Each User Story

- Tests first (written to fail before implementation).
- Schemas before services.
- Services before route handlers.
- Route handler changes last — they're facades (ADR-034).

### Parallel Opportunities

- T006 and T011 in Phase 2: different test files, independent.
- All test tasks marked `[P]` within a story phase: different files.
- US1, US2, and US3 can run fully in parallel after Phase 2 completes.
- US4 and US5 can run fully in parallel after US3 completes.
- All polish tasks marked `[P]` can run in parallel.

---

## Parallel Example: User Story 3 — Chip confirm flow

```bash
# Tests (independent files — write these first to fail):
Task T020: tests/core/taste/test_chip_merge.py
Task T021: tests/core/signal/test_service.py
Task T022: tests/api/routes/test_signal.py
Task T023: tests/api/routes/test_user_context.py

# Event class + pure function (independent files):
Task T024: src/kebi/core/events/events.py (ChipConfirmed)
Task T025: src/kebi/core/taste/chip_merge.py (merge_chip_statuses)
```

Once T024 / T025 land, T026 → T029 run sequentially (they touch `signal/service.py`, `api/schemas/signal.py`, `api/routes/signal.py`).

---

## Implementation Strategy

### MVP (User Story 1 only)

1. Phase 2 through T010 (T011 can wait if DB integration tests are delayed).
2. Phase 3 (US1).
3. Validate: `GET /v1/user/context` returns `signal_tier=cold` for a brand-new user. Product repo starts gating.
4. Deploy to preview, demo.

### Incremental Delivery

1. MVP (US1) — cold reporting ships.
2. US2 — warming blend ships.
3. US3 — chip confirmation ships; users lock in taste.
4. US4 — taste summary reflects confirmations.
5. US5 — active-tier filtering ships.
6. Phase 8 polish.

Each increment is an independent merge to `dev`. Rollback for any US is isolated to its own files.

### Solo Developer (this repo's reality)

Order: Phase 2 → US1 → US2 → US3 → US4 → US5 → Phase 8. Commit per task or per tight group (≤3 tasks). Commit format per `.claude/rules/git.md`: `feat(taste): derive_signal_tier #T005`.

---

## Format Validation

Spot-checks confirm the checklist format:

- ✅ `- [ ] T007 Add InteractionType.CHIP_CONFIRM = "chip_confirm" ... in src/kebi/db/models.py` — foundational, no `[P]` (edits shared file), no `[Story]`.
- ✅ `- [ ] T020 [P] [US3] Add tests/core/taste/test_chip_merge.py ...` — `[P]` independent file, `[US3]` story scope, file path present.
- ✅ `- [x] T005 Create src/kebi/core/taste/tier.py ...` — completed; still valid checklist format.
- ✅ `- [ ] T046 Write ADR-061 in docs/decisions.md ...` — polish phase, no `[Story]` (correct).

Every task has checkbox + ID + optional `[P]` + `[Story]` where required + file path. No violations.

---

## Summary

- **Total tasks**: 53 (T001 – T053).
- **Already completed from prior iteration**: 9 — T001, T002, T003, T004, T005, T006, T013, T014, T015, T034. Marked `[x]` above.
- **Per phase**: Foundational 11, US1 4, US2 4, US3 11, US4 9, US5 4, Polish 10.
- **Parallelizable tasks**: 24 marked `[P]`.
- **Independent tests per story**:
  - US1 — `GET /v1/user/context` on a zero-interaction user → `signal_tier=cold`, empty chips, zero LLM calls.
  - US2 — POST `/v1/consult` as a user with 3 saves → results respect 80/20 discovered:saved mix; `warming_blend` reasoning step present.
  - US3 — submit `chip_confirm` signal; `/v1/user/context` reflects updated statuses; tier advances to `active` when all pending chips resolved.
  - US4 — post-chip_confirm taste summary carries `[confirmed]` / `[rejected]` / `[N signals]` annotations; confirmed chips preserved across regen cycles.
  - US5 — active-tier consult excludes rejected-chip candidates; confirmed chips appear in reasoning steps.
- **Suggested MVP**: US1 alone. Unblocks all product-repo frontend gating decisions. No consult-response shape change needed for MVP.
