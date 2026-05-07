---
description: "Task list for SSE streaming mode on POST /v1/consult"
---

# Tasks: Streaming Recommendations via SSE

**Input**: Design documents from `/specs/001-consult-streaming/`
**Prerequisites**: plan.md (tech stack, structure), spec.md (user stories P1/P2), data-model.md (schemas), contracts/ (API contract)

**Format**: `- [ ] [TaskID] [P?] [Story] Description with file path`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and directory structure

- [X] T001 Create API schemas directory: `src/kebi/api/schemas/` with `__init__.py`
- [X] T002 Create routes directory: `src/kebi/api/routes/` with `__init__.py`
- [X] T003 Create consult service directory: `src/kebi/core/consult/` with `__init__.py`
- [X] T004 Create tests for consult: `tests/api/` and `tests/core/consult/` directories with `__init__.py` files

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Schemas and service infrastructure that ALL user stories depend on

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T005 [P] Create ConsultRequest, Location, SyncConsultResponse, PlaceResult, ReasoningStep Pydantic models in `src/kebi/api/schemas/consult.py`
- [X] T006 [P] Create LLMClientProtocol abstract base class in `src/kebi/core/consult/service.py` for type hints
- [X] T007 Add SYSTEM_PROMPT constant to `src/kebi/core/consult/service.py`: "You are Kebi, an AI place recommendation assistant. Answer the user's query helpfully and concisely."
- [X] T008 Create ConsultService class constructor in `src/kebi/core/consult/service.py` accepting LLMClientProtocol

**Checkpoint**: Foundational schemas and service structure ready - user story implementation can now begin in parallel

---

## Phase 3: User Story 1 - Real-Time AI Response Streaming (Priority: P1) 🎯 MVP

**Goal**: The AI's response appears word by word as it is generated, not waiting for the complete answer

**Independent Test**: Send POST /v1/consult with `"stream": true` and observe tokens arriving incrementally as separate SSE events with a final done signal

### Implementation for User Story 1

- [X] T009 [US1] Implement ConsultService.stream() async generator method in `src/kebi/core/consult/service.py` that:
  - Calls `self._llm.stream(SYSTEM_PROMPT, query)` with system prompt and user query
  - Emits `data: {"token": "..."}\\n\\n` per token from AI provider
  - Handles request.is_disconnected() to break early on client disconnect
  - Yields final `data: {"done": true}\\n\\n` when all tokens complete
  - Uses try/finally for resource cleanup of AI stream

- [X] T010 [US1] Create consult route handler in `src/kebi/api/routes/consult.py` that:
  - Accepts ConsultRequest body and FastAPI Request object
  - Returns StreamingResponse when stream=true
  - Sets media_type="text/event-stream"
  - Sets headers: Cache-Control: no-cache, X-Accel-Buffering: no
  - Calls service.stream() with proper parameters

- [X] T011 [US1] Create get_consult_service() dependency factory in `src/kebi/api/routes/consult.py` that:
  - Returns ConsultService instance with LLM client from get_llm("orchestrator")
  - Used via Depends() in route handler

- [X] T012 [US1] Register consult router in `src/kebi/api/main.py` to include routes from `consult.py`

- [X] T013 [US1] Create Bruno request file at `kebi-config/bruno/ai-service/consult-stream.bru` for manual testing of streaming mode with stream=true parameter

**Checkpoint**: User Story 1 (Real-Time Streaming) is fully functional and independently testable

---

## Phase 4: User Story 2 - Backward Compatible Synchronous Mode (Priority: P2)

**Goal**: Existing callers continue to work unchanged by defaulting to synchronous JSON response

**Independent Test**: Send POST /v1/consult without `"stream"` field (or with stream=false) and receive standard JSON response immediately

### Implementation for User Story 2

- [X] T014 [US2] Implement ConsultService.consult() async method in `src/kebi/core/consult/service.py` that:
  - Returns SyncConsultResponse stub with placeholder data
  - Includes primary recommendation, alternatives, and reasoning steps
  - Uses data from parameters (user_id, query, location if provided)

- [X] T015 [US2] Update consult route handler in `src/kebi/api/routes/consult.py` to:
  - Check if body.stream is True (or absent/False)
  - Call service.consult() when stream=false or absent
  - Return JSONResponse with service result via model_dump()
  - Maintain backward compatibility with existing callers

**Checkpoint**: User Stories 1 AND 2 both work independently - streaming and synchronous paths functional

---

## Phase 5: User Story 3 - Resource Cleanup on Client Disconnect (Priority: P1)

**Goal**: When client disconnects mid-stream, server cleans up streaming resources without leaking memory or dangling async operations

**Independent Test**: Initiate streaming request, disconnect before final event, verify all associated async resources are released and memory remains stable

### Implementation for User Story 3

- [X] T016 [US3] Verify ConsultService.stream() in `src/kebi/core/consult/service.py` uses:
  - `await request.is_disconnected()` to detect client disconnect during token streaming
  - `break` statement in token loop to exit immediately on disconnect
  - `try/finally` block to ensure AI stream is closed via async context manager
  - No dangling generator or async operations

- [X] T017 [US3] Create integration test in `tests/api/test_consult.py` for disconnect scenario:
  - Initiate StreamingResponse to /v1/consult with stream=true
  - Simulate client disconnect mid-stream via httpx.AsyncClient
  - Verify connection closes gracefully without resource leaks
  - Verify async generator cleanup is called

- [X] T018 [US3] Create integration test in `tests/api/test_consult.py` for concurrent disconnect:
  - Spawn 5+ concurrent streaming requests
  - Disconnect all clients simultaneously
  - Verify no resource leaks or lingering connections
  - Monitor memory stability (no growth trend)

**Checkpoint**: All user stories complete - streaming, synchronous, and disconnect handling functional and tested

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Verification and final quality checks

- [X] T019 [P] Create unit tests in `tests/core/consult/test_service.py`:
  - Test ConsultService.stream() with mocked LLM client
  - Mock Anthropic client to emit test tokens
  - Verify token events are formatted correctly as JSON
  - Verify done event is final event
  - Test disconnect behavior with is_disconnected() mock

- [X] T020 [P] Create unit tests in `tests/core/consult/test_service.py`:
  - Test ConsultService.consult() with mocked LLM client
  - Verify SyncConsultResponse structure matches schema
  - Test with location and without location parameters

- [X] T021 [P] Create integration tests in `tests/api/test_consult.py`:
  - Test POST /v1/consult with stream=true returns text/event-stream
  - Test POST /v1/consult without stream field returns JSON
  - Test POST /v1/consult with stream=false returns JSON
  - Test response headers are correct (Cache-Control, X-Accel-Buffering)

- [X] T022 Run verification commands and confirm all pass:
  - `poetry run pytest tests/api/test_consult.py tests/core/consult/test_service.py -v`
  - `poetry run ruff check src/ tests/`
  - `poetry run ruff format src/ tests/ --check`
  - `poetry run mypy src/`

- [X] T023 Update `docs/api-contract.md` to include example streaming request/response in `/v1/consult` section (if not already added)

- [X] T024 Verify Bruno collection request file (`kebi-config/bruno/ai-service/consult-stream.bru`) works against localhost:8000 with real streaming

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies - can start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1 - BLOCKS all user stories
- **Phase 3 (US1 - P1)**: Depends on Phase 2 - can start once foundation ready
- **Phase 4 (US2 - P2)**: Depends on Phase 2 - can start once foundation ready (independent of US1)
- **Phase 5 (US3 - P1)**: Depends on Phase 3 (needs streaming implementation) - should run after US1
- **Phase 6 (Polish)**: Depends on all user stories being implemented

### Within User Stories

- **US1 (Streaming)**: T009 → T010 → T011 → T012 → T013 (mostly sequential, endpoint depends on service)
- **US2 (Synchronous)**: T014 → T015 (sequential, route depends on service method)
- **US3 (Disconnect)**: T016 (verification, no implementation) → T017 → T018 (tests, can be parallel with [P])

### Parallel Opportunities

- **Phase 1**: All four directory creation tasks can run in parallel (T001-T004 marked [P] in spirit, different dirs)
- **Phase 2**: Schemas (T005) and protocol (T006) can run in parallel [P]
- **Phase 6 Polish**: Unit tests (T019, T020) can run in parallel [P], both use mocks and don't depend on integration
- **Phase 6 Polish**: Integration tests (T021) can run in parallel with unit tests [P]
- **Once Phase 2 Complete**: US1 and US2 can be developed in parallel by different developers (independent stories)

### Task Dependencies

```
T001-T004 (Setup - independent dirs)
    ↓
T005, T006, T007, T008 (Foundational - schemas, service base)
    ↓
    ├─→ T009, T010, T011, T012, T013 (US1 - Streaming)
    │       ↓
    │   T017, T018 (US3 - Disconnect tests, depends on streaming impl)
    │
    └─→ T014, T015 (US2 - Synchronous, independent of US1)

T019, T020, T021 (Tests - can run in parallel)
T022 (Verification - runs after all impl)
T023, T024 (Documentation - final polish)
```

---

## Parallel Example: After Phase 2 (Foundation Ready)

**Parallel Execution**: Two developers can work on different user stories simultaneously:

**Developer A - User Story 1 (Streaming)**:
```
T009 → T010 → T011 → T012 → T013
```

**Developer B - User Story 2 (Synchronous)**:
```
T014 → T015
```

Both complete independently, then Developer A adds US3 tests (T017-T018), both run verification (T022).

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001-T004)
2. Complete Phase 2: Foundational (T005-T008)
3. Complete Phase 3: User Story 1 (T009-T013)
4. **STOP and VALIDATE**: Test streaming independently with Bruno
5. Run unit tests for streaming (T019 partial: stream tests only)
6. Deploy streaming MVP if ready

### Incremental Delivery

1. Phases 1-2: Foundation complete → Can implement stories
2. Phase 3: User Story 1 (Streaming) → Test independently → MVP complete
3. Phase 4: User Story 2 (Synchronous) → Test independently → Backward compat added
4. Phase 5: User Story 3 (Disconnect) → Integration tests → Production ready
5. Phase 6: Polish, verify, document → Release ready

### Parallel Team Strategy

With two developers:

1. **Both together**: Complete Phases 1-2 (foundation)
2. **Once foundation ready** (after T008):
   - Developer A: Phase 3 (US1 - Streaming)
   - Developer B: Phase 4 (US2 - Synchronous)
3. **After both stories complete**:
   - Developer A: Phase 5 (US3 - Disconnect testing)
   - Both: Phase 6 (Polish & Verification)

---

## Notes

- All tasks use exact file paths for clarity
- [P] in header indicates parallelization opportunities within phases
- [Story] labels (US1, US2, US3) track which user story each task belongs to
- Each user story can be completed and tested independently before moving to next
- Disconnect cleanup (US3) depends on streaming implementation existing (US1)
- Synchronous mode (US2) is fully independent of streaming (US1)
- Verify commands must all pass before considering implementation complete
- See plan.md for constitution checks and architectural constraints
- All implementation must follow CLAUDE.md standards: Pydantic models at boundaries, no hardcoded model names, FastAPI Depends() patterns, mypy --strict compliance
