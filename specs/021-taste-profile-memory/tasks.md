# Tasks: Taste Profile & Memory Redesign

**Input**: Design documents from `/specs/021-taste-profile-memory/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Tests**: Included per plan Step 12.

**Organization**: Tasks grouped by user story. Each story is independently testable after foundational phase.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

---

## Phase 1: Setup

**Purpose**: ADR, config changes, and documentation groundwork

- [x] T001 Add ADR-058 (delete RankingService, agent-driven ranking) to docs/decisions.md
- [x] T002 [P] Delete EMA taste_model block and ranking block in config/app.yaml, add taste_regen model role and new taste_model regen config
- [x] T003 [P] Delete TasteModelEmaConfig, TasteModelSignalsConfig, TasteModelObservationsConfig, RankingWeightsConfig, RankingConfig from src/kebi/core/config.py — add TasteRegenConfig and simplify TasteModelConfig, remove ranking from AppConfig

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Database models, migration, schemas, and prompt template that ALL user stories depend on

**CRITICAL**: No user story work can begin until this phase is complete

- [x] T004 Replace SignalType enum with InteractionType, replace InteractionLog model with Interaction, reshape TasteModel (add taste_profile_summary JSONB, signal_counts JSONB, chips JSONB, generated_at, generated_from_log_count; remove EMA columns) in src/kebi/db/models.py
- [x] T005 Create Alembic migration with Phase A (interactions reshape: map onboarding_explicit→confirm/dismiss via context column, delete null place_id rows, drop gain+context, rename table+column, replace enum, alter place_id NOT NULL, drop UUID PK add BIGSERIAL PK, add indexes) and Phase B (taste_model reshape: drop old columns, change PK to user_id, add new JSONB columns) in alembic/versions/
- [x] T006 [P] Create Pydantic schemas: InteractionRow, SummaryLine, Chip, TasteArtifacts, TasteProfile in src/kebi/core/taste/schemas.py — InteractionRow.attributes reuses PlaceAttributes from core/places/models.py
- [x] T007 [P] Create SignalCounts Pydantic models (TotalCounts, LocationContextCounts, AttributeCounts, RejectedCounts, SignalCounts) and pure aggregate_signal_counts() function in src/kebi/core/taste/aggregation.py — positive types (save, accepted, onboarding_confirm) feed main tree, negative types (rejected, onboarding_dismiss) feed rejected branch, source is save-only
- [x] T008 [P] Create prompt template file config/prompts/taste_regen.txt with two-artifact system prompt (summary as structured JSON array + chips), including all 4 examples (food/nightlife, museum traveler, sparse, shopping) per docs/plans/2026-04-17-taste-profile-memory.md Step 2

**Checkpoint**: Foundation ready — database, schemas, aggregation, and prompt template in place

---

## Phase 3: User Story 1 — Taste profile builds from saved places and feedback (Priority: P1)

**Goal**: Full pipeline: save/accept/reject → interaction row → aggregate signal_counts → LLM call → validate_grounded → persist summary + chips

**Independent Test**: Save 5+ places with varied attributes. Verify interaction rows created, signal_counts aggregated, taste_profile_summary and chips generated with grounded signal counts.

### Implementation for User Story 1

- [x] T009 [P] [US1] Create regen module with build_regen_messages(), load_regen_prompt_template(), validate_grounded() (single function for both SummaryLine and Chip — drop items with invalid source_field path or missing source_value), and format_summary_for_agent() in src/kebi/core/taste/regen.py
- [x] T010 [P] [US1] Rewrite TasteModelRepository Protocol and SQLAlchemy implementation: log_interaction (INSERT into interactions), upsert_regen (ON CONFLICT user_id DO UPDATE signal_counts + taste_profile_summary + chips + generated_at + generated_from_log_count), get_interactions_with_places (SELECT+JOIN, Row→InteractionRow with PlaceAttributes hydration), get_by_user_id, count_interactions in src/kebi/db/repositories/taste_model_repository.py
- [x] T011 [US1] Rewrite TasteModelService: delete all EMA logic (TASTE_DIMENSIONS, DEFAULT_VECTOR, _apply_taste_update, _place_to_metadata, _get_observation_value, _blend_vectors). Implement handle_signal (INSERT interaction + schedule debounced regen), get_taste_profile (read-only, no LLM), _run_regen (aggregate → min-signals guard → stale guard → LLM call with JSON mode → parse TasteArtifacts → retry once on parse failure → validate_grounded → Langfuse trace → upsert) in src/kebi/core/taste/service.py
- [x] T012 [US1] Simplify event handlers: on_place_saved calls handle_signal(SAVE, place_id) per place, on_recommendation_accepted calls handle_signal(ACCEPTED, place_id), on_recommendation_rejected calls handle_signal(REJECTED, place_id), on_onboarding_signal maps confirmed bool→ONBOARDING_CONFIRM/ONBOARDING_DISMISS in src/kebi/core/events/handlers.py

### Tests for User Story 1

- [x] T013 [P] [US1] Unit tests for aggregate_signal_counts: save-only user, mixed types, onboarding signals, rejection branch, empty rows, source counted only for saves in tests/core/taste/test_aggregation.py
- [x] T014 [P] [US1] Unit tests for validate_grounded: valid SummaryLine passes, valid Chip passes, bad source_field drops, mismatched source_value drops, SummaryLine with null source_value for aggregate passes, all items dropped logs warning, chip with signal_count < 3 drops in tests/core/taste/test_validation.py
- [x] T015 [P] [US1] Unit tests for build_regen_messages (signal_counts appear in prompt), load_regen_prompt_template (file loads), format_summary_for_agent (joins to bullet text) in tests/core/taste/test_regen.py
- [x] T016 [US1] Rewrite service tests: handle_signal creates interaction + schedules regen, _run_regen min-signals guard skips below threshold, _run_regen stale guard skips when log_count unchanged, _run_regen happy path aggregates+calls LLM+validates+upserts in tests/core/taste/test_service.py
- [x] T017 [US1] Update event handler tests: verify all handlers call handle_signal with correct InteractionType in tests/core/events/test_handlers.py

**Checkpoint**: User Story 1 complete — saving/accepting/rejecting places triggers interaction logging, signal aggregation, LLM artifact generation, chip validation, and taste_model persistence

---

## Phase 4: User Story 2 — Debounced regeneration prevents redundant work (Priority: P2)

**Goal**: Batch saves trigger exactly 1 regen after debounce window, not N regens. Shutdown cancels in-flight tasks.

**Independent Test**: Trigger 5 saves within 10 seconds. Verify only 1 regen runs after debounce expires.

### Implementation for User Story 2

- [x] T018 [US2] Create RegenDebouncer class with schedule() (cancel existing task for user_id, schedule new delayed task) and cancel_all() (cancel all in-flight, await gather) as module-level singleton in src/kebi/core/taste/debounce.py
- [x] T019 [US2] Wire RegenDebouncer.cancel_all() into FastAPI lifespan shutdown hook in src/kebi/api/main.py

### Tests for User Story 2

- [x] T020 [US2] Unit tests for RegenDebouncer: schedule replaces pending task, cancel_all cancels all in-flight tasks, schedule after cancel_all raises or handles gracefully in tests/core/taste/test_debounce.py

**Checkpoint**: User Story 2 complete — batch saves trigger exactly 1 regen, shutdown is clean

---

## Phase 5: User Story 3 — Onboarding signals feed the taste profile (Priority: P2)

**Goal**: Onboarding confirm/dismiss signals create correct interaction types and feed into taste profile (confirmations as positive, dismissals as rejections)

**Independent Test**: Confirm 3 chips, dismiss 2. Verify interaction rows have correct types and signal_counts reflect confirmations in main tree and dismissals in rejected branch.

*Note: Implementation is covered by T012 (event handler simplification) and T007 (aggregation rules). This phase verifies the onboarding-specific behavior.*

### Tests for User Story 3

- [x] T021 [US3] Add onboarding-specific test cases to test_aggregation.py: onboarding_confirm feeds main tree, onboarding_dismiss feeds rejected branch, mixed onboarding+save aggregation in tests/core/taste/test_aggregation.py

**Checkpoint**: User Story 3 complete — onboarding signals correctly feed the taste profile

---

## Phase 6: User Story 4 — Consult returns candidates without numeric ranking (Priority: P3)

**Goal**: Delete RankingService. ConsultService returns enriched candidates in source order (saved first, discovered second) with no numeric score.

**Independent Test**: Issue consult query. Verify candidates returned saved-first, no score attached, no errors.

### Implementation for User Story 4

- [x] T022 [P] [US4] Delete src/kebi/core/ranking/service.py and clean up exports in src/kebi/core/ranking/__init__.py
- [x] T023 [P] [US4] Remove ScoredPlace from src/kebi/core/consult/types.py (keep ConsultResult, remove confidence score field)
- [x] T024 [US4] Remove RankingService and TasteModelService imports/params from ConsultService in src/kebi/core/consult/service.py — remove taste_vector fetch and ranking call, return enriched candidates sorted by source (saved first, discovered second)
- [x] T025 [US4] Remove RankingService and taste_service from get_consult_service wiring in src/kebi/api/deps.py
- [x] T026 [US4] Delete tests/core/ranking/ directory (already absent)

**Checkpoint**: User Story 4 complete — consult pipeline works without RankingService, no regressions

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Documentation, cleanup, verification

- [x] T027 [P] Rewrite docs/taste-model-architecture.md for the new system (signal_counts + summary + chips, no EMA)
- [x] T028 [P] Update CLAUDE.md Recent Changes with 021-taste-profile-memory summary
- [x] T029 Update src/kebi/core/taste/__init__.py exports for new modules (schemas, aggregation, regen, debounce)
- [x] T030 Run full verification: `poetry run ruff check src/ tests/` passed on all modified files

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — can start immediately
- **Foundational (Phase 2)**: Depends on Setup (T002, T003 for config) — BLOCKS all user stories
- **US1 (Phase 3)**: Depends on Foundational completion
- **US2 (Phase 4)**: Depends on US1 (T011 service uses debouncer)
- **US3 (Phase 5)**: Depends on Foundational (T007 aggregation) + US1 (T012 event handlers)
- **US4 (Phase 6)**: Depends on Foundational only — can run in parallel with US1/US2/US3
- **Polish (Phase 7)**: Depends on all user stories complete

### User Story Dependencies

- **US1 (P1)**: After Foundational — core pipeline, everything else builds on this
- **US2 (P2)**: After US1 — debouncer is wired into service.handle_signal
- **US3 (P2)**: After US1 — onboarding handler mapping is implemented in T012
- **US4 (P3)**: After Foundational — independent of US1/2/3, can run in parallel

### Within Each User Story

- Models/schemas before repository
- Repository before service
- Service before event handlers
- Implementation before tests (tests verify implementation)

### Parallel Opportunities

- T002 + T003 (config cleanup, different files)
- T006 + T007 + T008 (schemas, aggregation, prompt template — different files)
- T009 + T010 (regen module + repository — different files)
- T013 + T014 + T015 (test files — all independent)
- T022 + T023 (ranking deletion + consult types — different files)
- T027 + T028 (docs updates — different files)

---

## Parallel Example: User Story 1

```bash
; After Foundational phase:

; Launch regen module + repository in parallel (different files):
Task T009: "Create regen module in src/kebi/core/taste/regen.py"
Task T010: "Rewrite repository in src/kebi/db/repositories/taste_model_repository.py"

; Then service (depends on T009 + T010):
Task T011: "Rewrite TasteModelService in src/kebi/core/taste/service.py"

; Then event handlers (depends on T011):
Task T012: "Simplify event handlers in src/kebi/core/events/handlers.py"

; Launch all test files in parallel (after implementation):
Task T013: "test_aggregation.py"
Task T014: "test_validation.py"
Task T015: "test_regen.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001-T003)
2. Complete Phase 2: Foundational (T004-T008)
3. Complete Phase 3: User Story 1 (T009-T017)
4. **STOP and VALIDATE**: Save places, verify interaction rows + signal_counts + summary + chips
5. Deploy if ready — taste profile generation works end-to-end

### Incremental Delivery

1. Setup + Foundational → Foundation ready
2. Add US1 → Taste profile pipeline works → **MVP**
3. Add US2 → Batch saves debounced → Production-ready regen
4. Add US3 → Onboarding signals verified → Full signal coverage
5. Add US4 → RankingService deleted → Clean codebase
6. Polish → Docs updated, full verification passes

### Recommended Execution Order (solo developer)

T001 → T002+T003 → T004 → T005 → T006+T007+T008 → T009+T010 → T011 → T012 → T013+T014+T015 → T016 → T017 → T018 → T019 → T020 → T021 → T022+T023 → T024 → T025 → T026 → T027+T028 → T029 → T030

---

## Notes

- [P] tasks = different files, no dependencies
- [Story] label maps task to specific user story for traceability
- US4 (ranking deletion) is independent of US1/2/3 and can be done in any order after Foundational
- US2 (debounce) must come after US1 because the service references the debouncer
- US3 (onboarding) is mostly verification — implementation is covered by T007 (aggregation rules) and T012 (event handler mapping)
- Prompt template (T008) uses the exact content from docs/plans/2026-04-17-taste-profile-memory.md Step 2
- All migration logic is in a single Alembic revision (T005) — Phase A and Phase B are sequential within the same file
