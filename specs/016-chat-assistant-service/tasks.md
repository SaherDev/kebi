# Tasks: Chat Assistant Service

**Input**: Design documents from `/specs/016-chat-assistant-service/`  
**Prerequisites**: plan.md ✓, spec.md ✓, research.md ✓, data-model.md ✓, contracts/chat-assistant.md ✓

**Tests**: Included — spec FR-008 explicitly requires unit test coverage with mocked LLM.

**Organization**: Tasks are grouped by user story. All four user stories (US1–US4) are served by the same `ChatAssistantService` — the differentiation is in system prompt coverage quality. US1 delivers the core working service; US2–US4 validate and refine prompt coverage for additional query types.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2, US3, US4)

---

## Phase 1: Setup

**Purpose**: Create directory structure and empty init files before any implementation.

- [x] T001 Create `src/kebi/core/chat/__init__.py` (empty — marks domain module)
- [x] T002 [P] Create `tests/core/chat/__init__.py` (empty — marks test package)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Shared infrastructure that ALL user stories depend on — must complete before any story can be implemented.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [x] T003 Add `chat_assistant` role to `config/app.yaml` under `models:` — `provider: openai`, `model: gpt-4o-mini`, `max_tokens: 1024`, `temperature: 0.9`
- [x] T004 [P] Add `LLMUnavailableError` exception class to `src/kebi/api/errors.py` and register a 503 handler in `register_error_handlers()` with body `{"error_type": "llm_unavailable", "detail": "<reason>"}`
- [x] T005 [P] Create `src/kebi/api/schemas/chat_assistant.py` — `ChatRequest(user_id: str, message: str)` with `min_length=1` on `message`, and `ChatResponse(response: str)`

**Checkpoint**: Config, error class, and schemas are ready. Implementation can now begin.

---

## Phase 3: User Story 1 — Destination Food Scene Queries (Priority: P1) 🎯 MVP

**Goal**: A user can ask "What do you think about Tokyo for food?" or "What should I know about eating out in Bangkok as a first-timer?" and receive a direct, opinionated, locally-informed answer.

**Independent Test**: `POST /v1/chat-assistant` with `{"user_id": "test", "message": "What do you think about Tokyo for food?"}` returns HTTP 200 with a non-empty `response` string that is opinionated and specific — not generic travel-guide text.

### Implementation

- [x] T006 [US1] Implement `ChatAssistantService` in `src/kebi/core/chat/chat_assistant_service.py`:
  - Constructor: `self._llm = get_llm("chat_assistant")`
  - Method: `async def run(self, message: str, user_id: str) -> str`
  - System prompt: position as a knowledgeable food and dining advisor — direct, opinionated, no generic travel-guide language; covers destination food scenes, food culture, etiquette, and discovery strategies
  - Langfuse tracing: `lf = get_langfuse_client()`, `generation = lf.generation(name="chat_assistant", input={"user_id": user_id, "message": message}) if lf else None`, track generation end in both success and exception branches
  - On any exception from `self._llm.complete()`: raise `LLMUnavailableError(str(exc))`

- [x] T007 [US1] Create `src/kebi/api/routes/chat_assistant.py` — `router = APIRouter()`, `POST /chat-assistant` endpoint that calls `Depends(get_chat_assistant_service)` and returns `ChatResponse(response=await service.run(body.message, body.user_id))`

- [x] T008 [US1] Add `get_chat_assistant_service()` to `src/kebi/api/deps.py` — returns `ChatAssistantService()` (no constructor args beyond the injected LLM)

- [x] T009 [US1] Register `chat_assistant_router` in `src/kebi/api/main.py` — import and `router.include_router(chat_assistant_router, prefix="")`

### Tests for User Story 1

- [x] T010 [US1] Write unit tests in `tests/core/chat/test_chat_assistant_service.py`:
  - **Happy path**: patch `get_llm` to return an `AsyncMock` whose `complete()` returns `"Great choice."` → `service.run("message", "uid")` returns `"Great choice."`
  - **LLM failure**: patch `get_llm` → `complete()` raises `RuntimeError("timeout")` → `service.run()` raises `LLMUnavailableError`
  - **Langfuse tracing**: patch `get_langfuse_client` to return a mock → verify `generation.end()` is called after a successful run

**Checkpoint**: `POST /v1/chat-assistant` with a destination question returns an opinionated response. `poetry run pytest tests/core/chat/` passes.

---

## Phase 4: User Story 2 — Food Knowledge & Culture Queries (Priority: P1)

**Goal**: A user can ask "What's the difference between tonkotsu and shoyu ramen?" or "Is omakase worth it if I've never tried it?" and receive a clear, confident, committed answer — no "it depends" without a follow-on recommendation.

**Independent Test**: `POST /v1/chat-assistant` with `{"user_id": "test", "message": "Is omakase worth it if I've never tried it?"}` returns HTTP 200 with a response that clearly commits to yes or no with practical context.

**Note**: This story is served by the same service and system prompt as US1. The task here is to verify and refine the system prompt to ensure food knowledge/culture queries receive the right treatment.

### Implementation

- [x] T011 [US2] Review and refine the system prompt in `src/kebi/core/chat/chat_assistant_service.py` to ensure food knowledge/culture questions (ramen types, omakase, izakaya vs regular restaurant) receive direct, knowledgeable answers — add explicit guidance in the prompt that conceptual questions should be answered with a clear recommendation, not a list of trade-offs

### Tests for User Story 2

- [x] T012 [US2] Add parametrized acceptance scenario tests to `tests/core/chat/test_chat_assistant_service.py` — verify that with a mocked LLM, the messages array passed to `complete()` contains the system prompt text; assert system prompt includes key persona signals ("opinionated", "direct", or equivalent)

**Checkpoint**: System prompt explicitly covers food knowledge/culture query handling. Prompt structure verified in tests.

---

## Phase 5: User Story 3 — Practical Etiquette & Safety Queries (Priority: P2)

**Goal**: A user can ask "Is tipping expected at restaurants in Japan?" or "Is street food in Chiang Mai safe to eat?" and receive a clear yes/no with practical reasoning — no diplomatic hedging.

**Independent Test**: `POST /v1/chat-assistant` with `{"user_id": "test", "message": "Is tipping expected at restaurants in Japan?"}` returns HTTP 200 with a response that opens with a clear yes/no stance.

**Note**: Same service and system prompt. Task is to ensure the prompt explicitly instructs the LLM to commit to an answer on etiquette and safety questions.

### Implementation

- [x] T013 [US3] Refine the system prompt in `src/kebi/core/chat/chat_assistant_service.py` to explicitly cover etiquette and safety questions — add instruction that tipping, safety, and dining custom questions must be answered with a direct stance first, reasoning second; never "it depends" as a standalone answer

**Checkpoint**: System prompt handles etiquette and safety query types with appropriate directness instruction.

---

## Phase 6: User Story 4 — Discovery & Evaluation Strategy Queries (Priority: P3)

**Goal**: A user can ask "What's a good way to find local spots when I travel?" or "How do I know if a place is a tourist trap or legit?" and receive 2–3 specific, actionable heuristics — not generic advice.

**Independent Test**: `POST /v1/chat-assistant` with `{"user_id": "test", "message": "How do I know if a place is a tourist trap or legit?"}` returns HTTP 200 with a response that includes at least two specific, observable signals.

**Note**: Same service and system prompt. Task is to ensure the prompt covers meta/discovery questions with concrete heuristic answers.

### Implementation

- [x] T014 [US4] Refine the system prompt in `src/kebi/core/chat/chat_assistant_service.py` to cover discovery and evaluation strategy questions — add guidance that meta questions about finding good places or spotting tourist traps should be answered with 2–3 specific, testable heuristics rather than vague advice

**Checkpoint**: System prompt covers all four query type families. Service handles the full range of specified use cases.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Final verification, quality gates, and observability confirmation.

- [x] T015 [P] Add Bruno request file at `kebi-config/bruno/ai-service/chat-assistant.bru` — already created; verify it works against a running server (seq: 8, POST /v1/chat-assistant, test response status 200 and `response` field non-empty)
- [x] T016 [P] Run `poetry run ruff check src/` — fix any lint errors in new files (`chat_assistant_service.py`, `chat_assistant.py` route, `chat_assistant.py` schemas, `errors.py`, `deps.py`, `main.py`)
- [x] T017 [P] Run `poetry run mypy src/` — resolve any type errors; ensure `LLMUnavailableError` import is clean in route and service; verify `get_llm()` return type is used correctly
- [x] T018 Run `poetry run pytest tests/core/chat/` — all four test cases pass
- [x] T019 Verify done criteria from plan.md:
  - `ChatAssistantService.run(message, user_id)` returns a string ✓
  - `POST /v1/chat-assistant` registered and reachable ✓
  - LLM failure → HTTP 503 ✓
  - Empty message → HTTP 422 ✓
  - All tests pass ✓
  - Ruff clean ✓
  - Mypy clean (new files only; pgvector stubs error is pre-existing) ✓

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1 — **BLOCKS all user stories**
- **Phase 3 (US1)**: Depends on Phase 2 — delivers core working service (MVP)
- **Phase 4 (US2)**: Depends on Phase 3 (service must exist to refine its prompt)
- **Phase 5 (US3)**: Depends on Phase 4 (sequential prompt refinement)
- **Phase 6 (US4)**: Depends on Phase 5 (sequential prompt refinement)
- **Phase 7 (Polish)**: Depends on all phases complete

### Within Each Phase

- T001 and T002 are parallel (different files)
- T003, T004, T005 in Phase 2 are parallel after Phase 1 completes
- T006 → T007 → T008 → T009 (sequential within US1 — service must exist before route, route before deps, deps before main registration)
- T010 can be written in parallel with T007–T009 (tests only depend on T006 service)

---

## Notes

- All 4 user stories route through the same `ChatAssistantService.run()` — implementation is shared, differentiation is in system prompt quality
- `LLMUnavailableError` must be imported from `kebi.api.errors` — avoid circular imports (errors.py has no route imports)
- `get_langfuse_client()` returns `None` when Langfuse is not configured — always guard with `if lf:`
- `asyncio_mode = "auto"` in pytest config — no `@pytest.mark.asyncio` decorator needed
- Git comment character in this repo is `;` not `#`
