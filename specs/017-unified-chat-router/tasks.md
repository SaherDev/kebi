# Tasks: Unified Chat Router

**Input**: Design documents from `specs/017-unified-chat-router/`  
**Branch**: `017-unified-chat-router`

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story (US1–US4) this task belongs to

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Config change required before any code compiles.

- [x] T001 Add `intent_router` role to `config/app.yaml` under `models:` (`provider: groq`, `model: llama-3.1-8b-instant`, `max_tokens: 256`, `temperature: 0`)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Schemas, the intent router function, and the ConsultLogRepository Protocol must exist before any user story can be implemented. No story work starts until these are done.

⚠️ **CRITICAL**: Blocks all user story phases.

- [x] T002 [P] Create `src/kebi/api/schemas/chat.py` with `ChatRequest` (user_id: str, message: str, location: `Location | None` — import `Location` from `kebi.api.schemas.consult`) and `ChatResponse` (type: str, message: str, data: `dict[str, Any] | None`)
- [x] T003 [P] Create `IntentClassification` Pydantic model in `src/kebi/core/chat/router.py` (fields: intent: str, confidence: float, clarification_needed: bool, clarification_question: `str | None`)
- [x] T004 Implement `classify_intent(message: str) -> IntentClassification` in `src/kebi/core/chat/router.py`: call `get_llm("intent_router")`, use the system prompt from spec verbatim, attach Langfuse callback via `get_langfuse_client()`, parse response with `model_validate_json()`, on `ValidationError` strip markdown fences (` ```json ``` `) and retry once before re-raising (depends on T003)
- [x] T005 [P] Create `ConsultLogRepository` Protocol and `NullConsultLogRepository` stub (no-op `save()`) in `src/kebi/db/repositories/consult_log_repository.py` — real SQLAlchemy impl added in US4

**Checkpoint**: Config, schemas, intent router, and repository Protocol exist — user stories can now begin.

---

## Phase 3: User Story 1 — Core Routing & Dispatch (Priority: P1) 🎯 MVP

**Goal**: A single `POST /v1/chat` call classifies intent and dispatches to the correct pipeline, returning a typed response. All four happy-path intents work. Old endpoints coexist during this phase (removed in US3).

**Independent Test**: Send each of the four canonical messages and verify the `type` field matches the expected intent. Server must return 200 for all.

- [x] T006 [P] [US1] Implement `ChatService` in `src/kebi/core/chat/service.py`: constructor takes `ExtractionService`, `ConsultService`, `RecallService`, `ChatAssistantService` only (no repo — logging is ConsultService's responsibility); `run(request: ChatRequest) -> ChatResponse` dispatches by intent, wraps each service result in `ChatResponse` (note: `ChatAssistantService.run()` returns `str` — wrap as `message=result, data=None`); catch all exceptions and return `type="error"`
- [x] T007 [P] [US1] Create `src/kebi/api/routes/chat.py` with `router = APIRouter()` and `@router.post("/chat", status_code=200) async def chat(body: ChatRequest, service: ChatService = Depends(get_chat_service)) -> ChatResponse`
- [x] T008 [P] [US1] Add `async def get_chat_service(...)` to `src/kebi/api/deps.py` — inject all four existing service deps (no repo — logging is inside ConsultService); reuse `get_consult_service`, `get_recall_service`, `get_extraction_service`, `get_chat_assistant_service`
- [x] T009 [US1] Register `chat_router` in `src/kebi/api/main.py`: add `from kebi.api.routes.chat import router as chat_router` and `router.include_router(chat_router, prefix="")` (keep old routers for now — they are removed in US3)
- [x] T010 [P] [US1] Write unit tests for `classify_intent` in `tests/core/chat/test_router.py`: mock LLM return, verify each of the four intents parses correctly, verify markdown-fence stripping on malformed response
- [x] T011 [P] [US1] Write unit tests for `ChatService.run()` dispatch paths in `tests/core/chat/test_service.py`: mock all four services and repo, assert correct `type` and `message` for each intent, assert `type="error"` on downstream exception
- [x] T012 [P] [US1] Write route tests for `POST /v1/chat` in `tests/api/routes/test_chat.py`: mock `ChatService`, assert 200 response shape for each intent type

**Checkpoint**: `POST /v1/chat` is live and routes all four intents correctly. Old endpoints still work in parallel.

---

## Phase 4: User Story 2 — Clarification on Ambiguous Input (Priority: P2)

**Goal**: Messages with intent confidence < 0.7 return `type="clarification"` with a single question instead of dispatching to a pipeline.

**Independent Test**: Send `"fuji"` — response must be `{ "type": "clarification", "message": "<question>", "data": null }`. No pipeline call occurs.

> The clarification branch is implemented inside `ChatService.run()` step 2 (already present from US1). This phase explicitly verifies and tests that branch in isolation.

- [x] T013 [P] [US2] Add low-confidence test cases to `tests/core/chat/test_router.py`: mock LLM returning `confidence=0.48, clarification_needed=true` — assert `IntentClassification.clarification_needed` is `True` and `clarification_question` is non-null
- [x] T014 [P] [US2] Add clarification response test to `tests/core/chat/test_service.py`: mock `classify_intent` returning `clarification_needed=True` — assert `ChatService.run()` returns `ChatResponse(type="clarification", message=<question>, data=None)` without calling any downstream service
- [x] T015 [US2] Add `"fuji"` end-to-end test to `tests/api/routes/test_chat.py`: mock `ChatService.run` returning a clarification response — assert route returns `{"type": "clarification", "data": null}`

**Checkpoint**: Ambiguous messages are caught before dispatch. Zero silent misrouting.

---

## Phase 5: User Story 3 — Remove Old Endpoints (Priority: P3)

**Goal**: All four old endpoints return 404. Only `/v1/chat` handles conversational traffic.

**Independent Test**: `POST /v1/extract-place`, `POST /v1/consult`, `POST /v1/recall`, `POST /v1/chat-assistant` all return 404.

⚠️ Complete US1 (Phase 3) before this phase — verify `POST /v1/chat` is working before removing old routes.

- [x] T016 [P] [US3] Delete `src/kebi/api/routes/extract_place.py`
- [x] T017 [P] [US3] Delete `src/kebi/api/routes/consult.py`
- [x] T018 [P] [US3] Delete `src/kebi/api/routes/recall.py`
- [x] T019 [P] [US3] Delete `src/kebi/api/routes/chat_assistant.py`
- [x] T020 [US3] Remove all deleted router imports and `router.include_router(...)` calls from `src/kebi/api/main.py` for `chat_assistant_router`, `consult_router`, `extract_place_router`, `recall_router` (keep `feedback_router`)

**Checkpoint**: Only `/v1/chat`, `/v1/feedback/*`, and `/v1/health` are registered. Old endpoints return 404.

---

## Phase 6: User Story 4 — Recommendation Persistence (Priority: P4)

**Goal**: Every completed consult response writes one `consult_logs` record. Write failures are logged, not propagated.

**Independent Test**: Send a consult message via `/v1/chat`, query `SELECT * FROM consult_logs` — one row exists with matching `user_id`, `query`, `intent="consult"`.

- [x] T021 [P] [US4] Add `ConsultLog` SQLAlchemy model to `src/kebi/db/models.py` — table `"consult_logs"`, fields: `id` (PGUUID PK, default uuid4), `user_id` (String, indexed), `query` (Text), `response` (JSONB), `intent` (String), `accepted` (Boolean nullable), `selected_place_id` (String nullable, no FK constraint), `created_at` (DateTime tz, server_default now())
- [x] T022 [P] [US4] Implement `SQLAlchemyConsultLogRepository` in `src/kebi/db/repositories/consult_log_repository.py` — `save(log: ConsultLog) -> None` using `session.add(log); await session.commit()`; export both Protocol and impl from the file
- [x] T023 [US4] Run `poetry run alembic revision --autogenerate -m "add_consult_logs_table"` — review the generated file in `alembic/versions/` and confirm only `consult_logs` table is created
- [x] T024 [US4] Run `poetry run alembic upgrade head` — confirm migration applies cleanly against running postgres (depends on T023)
- [x] T025 [US4] Add `get_consult_log_repo()` to `src/kebi/api/deps.py`: inject `AsyncSession` via `Depends(get_session)`, return `SQLAlchemyConsultLogRepository(session)`; update `get_consult_service()` to also accept this dep and pass it into `ConsultService.__init__` (depends on T022, T024)
- [x] T026 [US4] Add `ConsultLogRepository` as a constructor dep to `ConsultService` in `src/kebi/core/consult/service.py`; at the end of the `consult()` method, after building the response, call `await self._consult_log_repo.save(log)` inside a `try/except` that logs the failure and returns the response regardless (depends on T021, T025)

**Checkpoint**: Every consult response produces exactly one `consult_logs` row. DB write failures do not fail the response.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: ADRs, API contract, Bruno, and full verification pass.

- [x] T027 [P] Add ADR-052 entry at the top of `docs/decisions.md`: "Consolidate routes into routes/chat.py — supersedes ADR-018. Context: Feature 017 unified /v1/chat replaces four individual route modules. Routes/extract_place.py, consult.py, recall.py, chat_assistant.py are deleted. Decision: routes/chat.py is the single route module for conversational API traffic."
- [x] T028 [P] Add ADR-053 entry at the top of `docs/decisions.md`: "This repo owns consult_logs table for AI recommendation history — distinct from NestJS recommendations table (Constitution Section VI). Context: Feature 017 needs to persist consult results for feedback loops. Decision: Table named consult_logs (not recommendations) to avoid write-ownership conflict with NestJS."
- [x] T029 [P] Update `docs/api-contract.md` to reflect the /v1/chat contract: replace the extract-place and consult sections with the contract from `specs/017-unified-chat-router/contracts/chat.md`; note status polling endpoint (GET /v1/extract-place/status/{id}) is deferred
- [ ] T030 [P] Delete stale Bruno files from `kebi-config/bruno/ai-service/`: `chat-assistant.bru`, `consult.bru`, `extract-place.bru`, `extract-place-status.bru`, `recall.bru` — BLOCKED: kebi-config directory has read-only permissions in sandbox; must be done manually
- [ ] T031 [P] Create `kebi-config/bruno/ai-service/chat.bru` with 5 request bodies: (1) `"cheap dinner nearby"` with location, (2) TikTok URL, (3) `"that ramen place I saved"`, (4) `"is tipping expected in Japan?"`, (5) `"fuji"` (no location) — BLOCKED: kebi-config directory has read-only permissions in sandbox; must be done manually
- [x] T032 Run full verification suite: `poetry run pytest` (all pass), `poetry run ruff check src/ tests/` (zero errors in new files; 14 pre-existing errors remain), `poetry run mypy src/` (6 pre-existing errors remain; zero new errors introduced)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1 — **BLOCKS all user story phases**
- **Phase 3 (US1)**: Depends on Phase 2 — MVP deliverable
- **Phase 4 (US2)**: Depends on Phase 2 — can run in parallel with US1 (different test files)
- **Phase 5 (US3)**: Depends on Phase 3 (US1 must be working before old endpoints are removed)
- **Phase 6 (US4)**: Depends on Phase 2 — can start in parallel with US1; T026 depends on T006
- **Phase 7 (Polish)**: Depends on all user story phases

### User Story Dependencies

- **US1 (P1)**: Depends on Phase 2 only
- **US2 (P2)**: Depends on Phase 2 only (test-only phase; clarification logic is in ChatService from US1)
- **US3 (P3)**: Depends on US1 being complete and verified
- **US4 (P4)**: Depends on Phase 2; T026 (wiring save()) depends on T006 (ChatService)

### Within Each Phase

- Tasks marked [P] within the same phase can run in parallel
- Models before services; services before endpoints; endpoints before integration

---

## Parallel Execution Examples

### Phase 2 (Foundational) — run together
```
T002: Create api/schemas/chat.py
T003: Create IntentClassification model in core/chat/router.py
T005: Create ConsultLogRepository Protocol in db/repositories/
```
Then sequentially: T004 (implements classify_intent, depends on T003)

### Phase 3 (US1) — run together after T004
```
T006: ChatService in core/chat/service.py
T007: Route in api/routes/chat.py
T008: Deps in api/deps.py
T010: Tests for classify_intent
T011: Tests for ChatService
T012: Tests for route
```
Then sequentially: T009 (main.py registration, depends on T007, T008)

### Phase 6 (US4) — run together
```
T021: ConsultLog model in db/models.py
T022: SQLAlchemyConsultLogRepository in db/repositories/
```
Then sequentially: T023 → T024 → T025 → T026

---

## Implementation Strategy

### MVP (User Story 1 only)

1. Phase 1: T001 (config)
2. Phase 2: T002 → T005 (schemas + router + protocol)
3. Phase 3: T006 → T012 (ChatService + route + deps + tests)
4. **STOP**: Verify `POST /v1/chat` routes all 4 intents correctly
5. Deploy or demo if ready

### Incremental Delivery

1. **Phase 1–3**: `/v1/chat` routing live (old endpoints coexist)
2. **Phase 4**: Clarification verified
3. **Phase 5**: Old endpoints removed — API surface cleaned
4. **Phase 6**: `consult_logs` persistence active
5. **Phase 7**: ADRs, docs, Bruno, full test pass

---

## Task Summary

| Phase | Tasks | Story |
|---|---|---|
| Phase 1: Setup | T001 | — |
| Phase 2: Foundational | T002–T005 | — |
| Phase 3: US1 Core Routing | T006–T012 | US1 |
| Phase 4: US2 Clarification | T013–T015 | US2 |
| Phase 5: US3 Remove Old Endpoints | T016–T020 | US3 |
| Phase 6: US4 Persistence | T021–T026 | US4 |
| Phase 7: Polish | T027–T032 | — |
| **Total** | **32 tasks** | |

**Parallel opportunities**: 18 tasks marked [P]  
**MVP scope**: T001–T012 (Phases 1–3, 12 tasks)
