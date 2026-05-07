---
description: "Task list for 008-taste-model feature implementation"
---

# Tasks: Taste Model Implementation

**Input**: Design documents from `specs/008-taste-model/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/feedback-endpoint.md

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3, US4)
- Include exact file paths in descriptions

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Configuration and database schema preparation. Blocking prerequisites for all user stories.

- [ ] T001 Add taste_model and ranking sections to config/app.yaml with all ema α values, signal gains, and observation lookup table
- [ ] T002 Create Alembic migration to rename taste_model.performance_score → eval_score and add confidence (Float, default 0.0) + interaction_count (Integer, default 0) columns in `alembic/versions/`
- [ ] T003 Create Alembic migration to create interaction_log table with id (UUID PK), user_id (indexed), signal_type (enum), place_id (nullable FK), gain (Float), context (JSONB), created_at (timestamp) in `alembic/versions/`
- [ ] T004 [P] Update db/models.py: add confidence, interaction_count, eval_score columns to TasteModel ORM class
- [ ] T005 [P] Create InteractionLog ORM class in db/models.py with Python Enum for signal_type supporting all 7 signal types

**Checkpoint**: Configuration loaded, database schema migrated, ORM models ready

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented

⚠️ **CRITICAL**: No user story work can begin until this phase is complete

- [ ] T006 Create TasteModelRepository Protocol in src/kebi/db/repositories/taste_model_repository.py with methods: get_by_user_id(user_id) → TasteModel | None, upsert(user_id, parameters, confidence, interaction_count) → TasteModel, log_interaction(user_id, signal_type, place_id, gain, context) → None
- [ ] T007 Implement TasteModelRepository concrete SQLAlchemy class in same file with atomic SQL increment for interaction_count updates and abort-on-failure semantics for log writes
- [ ] T008 Export TasteModelRepository from src/kebi/db/repositories/__init__.py
- [ ] T009 [P] Create DomainEvent base class and concrete event models (PlaceSaved, RecommendationAccepted, RecommendationRejected, OnboardingSignal) in src/kebi/core/events/events.py
- [ ] T010 [P] Create EventDispatcherProtocol and EventDispatcher concrete implementation in src/kebi/core/events/dispatcher.py, accepting BackgroundTasks and handler registry, implementing dispatch(event) → None
- [ ] T011 Create event handler functions (on_place_saved, on_recommendation_accepted, on_recommendation_rejected, on_onboarding_signal) in src/kebi/core/events/handlers.py with try/except logging and Langfuse tracing for failures

**Checkpoint**: Repository pattern implemented, event system ready, handlers defined

---

## Phase 3: User Story 1 - Recommendations improve as user saves more places (Priority: P1) 🎯 MVP

**Goal**: Saves trigger taste model updates, confidence increases, and users with 10+ saves receive personalized recommendations.

**Independent Test**: Save 5 places in the same cuisine/price range. Verify interaction_log entries created with signal_type "save" and gain 1.0. Verify taste_model.confidence increases and interaction_count increments. Verify get_taste_vector() returns stored vector (not defaults) for user with 10+ saves.

### Implementation for User Story 1

- [ ] T012 [US1] Create TasteModelService in src/kebi/core/taste/service.py with handle_place_saved(user_id, place_id, place_metadata) method implementing: log_interaction(signal_type="save", gain=1.0), EMA update for all 8 dimensions using positive formula, confidence recomputation
- [ ] T013 [US1] Implement get_taste_vector(user_id) → dict[str, float] in TasteModelService applying personalization routing: 0 interactions → all-0.5 defaults, 1–9 interactions → 40/60 blend, ≥10 interactions → stored vector
- [ ] T014 [US1] Update ExtractionService in src/kebi/core/extraction/service.py to accept event_dispatcher: EventDispatcherProtocol, dispatch PlaceSaved event after _place_repo.save() succeeds and before embedding block (document BackgroundTasks failure mode choice in code comment)
- [ ] T015 [US1] Update src/kebi/api/deps.py: create get_event_dispatcher(background_tasks: BackgroundTasks, db_session) dependency that constructs TasteModelService, builds handler registry mapping event types → handler functions, returns EventDispatcher instance
- [ ] T016 [US1] Update get_extraction_service in src/kebi/api/deps.py to accept event_dispatcher: EventDispatcherProtocol via Depends(get_event_dispatcher), pass to ExtractionService constructor

**Checkpoint**: User Story 1 complete and independently testable. Saves trigger taste model updates and confidence routing works.

---

## Phase 4: User Story 2 - Onboarding actions immediately seed a taste profile (Priority: P2)

**Goal**: Onboarding confirmations/dismissals create taste signals with positive/negative gains, enabling personalized first recommendation.

**Independent Test**: New user confirms 3 taste chips and dismisses 2. Verify interaction_log entries created with signal_type "onboarding_explicit", gains 1.2 (confirmed) and -0.8 (dismissed). Verify get_taste_vector() reflects confirmed preferences and not dismissed ones.

### Implementation for User Story 2

- [ ] T017 [P] [US2] Implement handle_onboarding_signal(user_id, place_id, confirmed: bool) in TasteModelService: confirmed=True → signal_type="onboarding_explicit", gain=1.2; confirmed=False → signal_type="onboarding_explicit", gain=-0.8. Use positive formula for confirmed, negative formula for dismissed.
- [ ] T018 [US2] Create on_onboarding_signal handler in src/kebi/core/events/handlers.py dispatching to TasteModelService.handle_onboarding_signal()
- [ ] T019 [US2] Register on_onboarding_signal handler in EventDispatcher handler registry in src/kebi/api/deps.py

**Checkpoint**: User Story 2 complete and independently testable. Onboarding signals create taste updates with correct gain signs.

---

## Phase 5: User Story 3 - Accepting or rejecting a recommendation shapes future ones (Priority: P3)

**Goal**: Recommendation feedback (acceptance/rejection) creates explicit taste signals with gain 2.0 and -1.5 respectively, wired via POST /v1/feedback endpoint.

**Independent Test**: User rejects 3 consecutive recommendations for cuisine A. Verify interaction_log entries created with signal_type "rejected" and gain -1.5. Verify cuisine_frequency dimension decreases (via negative EMA formula). Next consultation shows reduced weighting for cuisine A.

### Implementation for User Story 3

- [ ] T020 [US3] Implement handle_recommendation_accepted(user_id, place_id) in TasteModelService: signal_type="accepted", gain=2.0, use positive EMA formula
- [ ] T021 [US3] Implement handle_recommendation_rejected(user_id, place_id) in TasteModelService: signal_type="rejected", gain=-1.5, use negative EMA formula
- [ ] T022 [US3] Create on_recommendation_accepted and on_recommendation_rejected handlers in src/kebi/core/events/handlers.py dispatching to TasteModelService methods
- [ ] T023 [US3] Register both handlers in EventDispatcher handler registry in src/kebi/api/deps.py
- [ ] T024 [US3] Create POST /v1/feedback route handler in src/kebi/api/routes/feedback.py: accept FeedbackRequest (user_id, recommendation_id, place_id, signal: "accepted" | "rejected"), dispatch RecommendationAccepted or RecommendationRejected event, return FeedbackResponse with status: "received"
- [ ] T025 [US3] Include feedback_router in src/kebi/api/main.py under app.include_router(feedback_router, prefix="/v1")

**Checkpoint**: User Story 3 complete and independently testable. POST /v1/feedback endpoint wired to taste model updates.

---

## Phase 6: User Story 4 - Taste profile confidence drives personalization routing (Priority: P4)

**Goal**: Confidence score increases monotonically with interaction_count and drives routing logic, ensuring early users get defaults while power users get personalized vectors.

**Independent Test**: Create user with 0, 5, 10, 20 interactions. Verify confidence = 1 − e^(−count / 10) at each checkpoint. Verify 0-interaction user receives all-0.5 defaults. Verify 5-interaction user receives 40/60 blend. Verify 20-interaction user receives stored vector. Verify stored vectors differ from zero-interaction defaults.

### Implementation for User Story 4

- [ ] T026 [P] [US4] Verify confidence recomputation in all TasteModelService handlers (implement in T012, T017, T020, T021): after interaction_count increment, compute new_confidence = 1 − e^(−new_count / 10), write to taste_model.confidence column
- [ ] T027 [US4] Verify get_taste_vector routing logic returns correct vector per interaction_count threshold: 0 → defaults, 1–9 → blend, ≥10 → stored
- [ ] T028 [US4] Verify ConsultService reads taste vector and applies it in ranking (implemented in T029)

**Checkpoint**: User Story 4 integrated throughout all phases. Confidence routing verified.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Ranking integration and final quality checks

- [ ] T029 [P] Implement taste_similarity metric in RankingService in src/kebi/core/ranking/service.py: create _compute_taste_similarity(candidate_place, taste_vector) method that maps place metadata (cuisine, price_range, ambiance, distance, time_of_day, dietary, crowd, adventurousness) to 8-dimension observation vector, returns dot-product similarity score [0, 1]
- [ ] T030 [P] Create RankingService.rank(candidates, taste_vector) method in src/kebi/core/ranking/service.py reading all weights from config/app.yaml ranking.weights, computing final score = taste_similarity × w_taste + distance_score × w_distance + price_fit_score × w_price + popularity_score × w_popularity (no hardcoded floats), returning candidates sorted descending by score
- [ ] T031 [P] Update ConsultService in src/kebi/core/consult/service.py to call TasteModelService.get_taste_vector(user_id), pass vector to RankingService.rank()
- [ ] T032 Create .bru request file for POST /v1/feedback in kebi-config/bruno/ documenting endpoint contract
- [ ] T033 Run all verify commands: `poetry run pytest` (all tests pass), `poetry run ruff check src/` (no lint errors), `poetry run mypy src/` (no type errors)
- [ ] T034 Verify data integrity: query interaction_log for signal_type distribution, confirm all interaction_count values match log replay, confirm all gain values match config snapshot at write time

**Checkpoint**: All user stories complete, code quality verified, ready for demo

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies - start immediately. Blocks Phase 2.
- **Foundational (Phase 2)**: Depends on Setup completion. BLOCKS all user story phases.
- **User Stories (Phases 3-6)**: All depend on Foundational completion.
  - US1 (Phase 3) can start immediately after Foundational
  - US2 (Phase 4) can start after Phase 3 complete OR in parallel (independent)
  - US3 (Phase 5) can start after Phase 3 complete OR in parallel (independent)
  - US4 (Phase 6) integrated throughout Phases 3-5
- **Polish (Phase 7)**: Depends on all user stories (Phases 3-6) complete

### User Story Dependencies

- **US1**: Depends on Phase 2 only - No cross-story dependencies
- **US2**: Depends on Phase 2 only - Can start in parallel with US1
- **US3**: Depends on Phase 2 only - Can start in parallel with US1/US2
- **US4**: Integrated into US1/US2/US3 implementations - No separate work required

### Within Each Phase

- Setup tasks T001-T005: Run T001 first (config), then T002-T003 (migrations can run in sequence), T004-T005 in parallel
- Foundational tasks T006-T011: T006-T008 sequential (repository definition → implementation → export), T009-T010 in parallel (events and dispatcher), T011 last (handlers depend on event models)
- User Story phases: Tests first (if any), then models/services, then endpoints, then integration

### Parallel Opportunities

#### Setup (Phase 1)
- T004 and T005 can run in parallel (both ORM updates to different classes)

#### Foundational (Phase 2)
- T009 and T010 can run in parallel (events models and dispatcher implementation are independent)

#### User Story 1 (Phase 3)
- T012 and T013 can run in parallel (both TasteModelService methods, different concerns)

#### User Story 2 (Phase 4)
- T017 can run in parallel with other foundational work if T012/T013 are complete

#### User Story 3 (Phase 5)
- T020 and T021 can run in parallel (two handler methods)
- T022 can start after both handlers drafted (minimal additional work)

#### User Story 4 (Phase 6)
- T026 and T027 can run in parallel (verification tasks on different code paths)

#### Polish (Phase 7)
- T029 and T030 can run in parallel (RankingService and ConsultService integration)

---

## Parallel Example: Aggressive Parallelization (After Foundational Complete)

If staffed with 3+ developers and Foundational (Phase 2) complete:

```bash
Developer A (US1 - Saves):
- T012: handle_place_saved (TasteModelService)
- T013: get_taste_vector routing
- T014: ExtractionService dispatch wiring
- T015-T016: deps.py event dispatcher integration

Developer B (US2 - Onboarding):
- T017: handle_onboarding_signal
- T018-T019: onboarding handlers
(Can start after T012 foundation exists)

Developer C (US3 - Feedback):
- T020-T021: handle_recommendation_accepted/rejected
- T022-T025: feedback endpoint
(Can start after T012 foundation exists)

All (Polish):
- T029-T031: RankingService taste_similarity + rank method + ConsultService integration (after all stories)
- T032-T034: Quality checks
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

**Rationale**: US1 is P1 and highest value - users see personalization from saves immediately.

1. Complete Phase 1: Setup (config + migrations + ORM)
2. Complete Phase 2: Foundational (repository + events + handlers)
3. Complete Phase 3: User Story 1 (TasteModelService + ExtractionService wiring)
4. **STOP and VALIDATE**: 
   - Verify saved place creates interaction_log entry
   - Verify taste_model.confidence updates correctly
   - Verify get_taste_vector returns correct vector per interaction count
   - Run full test suite
5. Deploy MVP

### Incremental Delivery (All User Stories)

1. Complete Phase 1 + 2 → Foundation ready (can't skip - blocks everything)
2. Add Phase 3 (US1) → Saves work → Deploy MVP
3. Add Phase 4 (US2) → Onboarding works → Deploy incremental
4. Add Phase 5 (US3) → Feedback works → Deploy incremental
5. Phase 6 (US4) → Integrated throughout, verified in Phase 7
6. Complete Phase 7 (Polish) → Full feature ready → Deploy final

### Parallel Team Strategy

With 3 developers, after Foundational complete:

1. Developer A: Complete Phase 3 (US1)
2. Developer B: Complete Phase 4 (US2) in parallel
3. Developer C: Complete Phase 5 (US3) in parallel
4. All: Complete Phase 7 (Polish) together
5. Phase 6 (US4) verified throughout

---

## Task Summary

| Phase | Count | Critical Path | Notes |
|-------|-------|---------------|-------|
| Phase 1 (Setup) | 5 tasks | Yes | Blocks Phase 2 |
| Phase 2 (Foundational) | 6 tasks | Yes | Blocks all user stories |
| Phase 3 (US1 - P1) | 5 tasks | Yes (MVP) | Deliverable on its own |
| Phase 4 (US2 - P2) | 3 tasks | No | Can run in parallel with US1 |
| Phase 5 (US3 - P3) | 4 tasks | No | Can run in parallel with US1/US2 |
| Phase 6 (US4 - P4) | 3 tasks | No | Integrated into other phases |
| Phase 7 (Polish) | 6 tasks | No | Final quality gate |
| **TOTAL** | **34 tasks** | — | 11 tasks on critical path (Phases 1-2) |

---

## Success Criteria Checklist

By end of Phase 7, verify:

- [ ] `poetry run pytest` passes 100%
- [ ] `poetry run ruff check src/` passes (no errors/warnings)
- [ ] `poetry run mypy src/` passes with `--strict`
- [ ] Alembic migrations run clean: `alembic upgrade head`
- [ ] User Story 1: Save place → interaction_log entry created → confidence updated
- [ ] User Story 2: Onboarding signal → interaction_log entry created → taste updated
- [ ] User Story 3: POST /v1/feedback → event dispatched → taste updated
- [ ] User Story 4: Confidence = 1 − e^(−count / 10) at each checkpoint
- [ ] get_taste_vector() routing verified for 0, 5, 10, 20 interaction counts
- [ ] ConsultService uses taste vector in ranking
- [ ] All gain values stored at write time (config changes don't rewrite log)
- [ ] Atomic SQL increment prevents concurrent update race conditions
- [ ] Log write failure aborts cache update (strict consistency)
- [ ] API error responses documented in docs/api-contract.md
- [ ] EventDispatcher failures logged and traced via Langfuse (not user-facing)

---

## Notes

- [P] markers identify fully parallelizable tasks (different files, zero dependencies)
- [Story] labels map tasks to user stories for traceability
- Each user story independently completable after Phase 2 (Foundational)
- Setup + Foundational = 11 tasks on critical path (3-4 hours if unblocked)
- Each User Story = 3-5 hours implementation
- Phase 7 Polish = 2-3 hours (mostly verification)
- Estimated total wall-clock time (single developer): 15-19 hours (includes taste_similarity metric)
- Recommended cadence: Phase 1-2 together, then US1 (demo), then US2+US3 (parallel), then Polish
- Taste similarity uses dot-product similarity between place metadata and taste vector (no neighbors/clustering in current scope)
