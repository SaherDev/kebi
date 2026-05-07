---

description: "Task list — Agent Tools & Chat Wiring (028-agent-tools-wiring)"
---

# Tasks: Agent Tools & Chat Wiring (M4 + M5 + M6)

**Input**: Design documents from `/specs/028-agent-tools-wiring/`
**Prerequisites**: plan.md ✓, spec.md ✓, research.md ✓, data-model.md ✓, contracts/ ✓, quickstart.md ✓

**Tests**: INCLUDED — the spec's FR-035 explicitly requires tests for every slice. Test tasks are written first within each story; they FAIL until the matching implementation lands.

**Source of truth**: `docs/plans/2026-04-21-agent-tool-migration.md` (milestones M4 / M5 / M6) plus the three plan-doc revisions landed in planning (EmitFn Protocol with `duration_ms`, `ConsultResponse.reasoning_steps` deletion, one-tool-call-per-response prompt edit).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on incomplete tasks)
- **[Story]**: `US1` / `US2` / `US3` — Setup / Foundational / Polish phases have NO story label
- Every task includes an exact file path

## Path Conventions

Single-project `src/` layout per ADR-001. Tests mirror `src/` under `tests/`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Confirm the feature-027 baseline is green before editing.

- [X] T001 Verify feature-027 baseline is green: `poetry run ruff check src/ tests/ && poetry run mypy src/ && poetry run pytest -x` at repo root. Record the baseline test count in the commit message of the first change. Any failure here is fixed before proceeding (the 028 work must not be confused with latent 027 breakage).

**Checkpoint**: Baseline recorded. No new dependencies to install (`poetry install` is a no-op vs 027's lockfile — research item 0).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented. `EmitFn` + `ReasoningStep.duration_ms` + the `PlaceFilters` family are shared across all three stories.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T002 [P] Create `src/kebi/core/emit.py` — define `EmitFn` as a `typing.Protocol` with `__call__(step: str, summary: str, duration_ms: float | None = None) -> None`. One small module; verbatim from data-model.md §0.
- [X] T003 [P] Edit `src/kebi/core/agent/reasoning.py` — add the `duration_ms: float | None = None` field to `ReasoningStep` between `timestamp` and the `@model_validator`. Update the class docstring to note that `duration_ms` is populated by the service or by the wrapper closure from timestamp deltas, and is always non-null in persisted steps. The existing `_source_tool_name_consistency` validator is unchanged.
- [X] T004 [P] Create `src/kebi/core/places/filters.py` — add `PlaceFilters` (Pydantic `BaseModel` mirroring `PlaceObject` 1:1: `place_type`, `subcategory`, `tags_include`, `attributes: PlaceAttributes | None`, `source`) and `ConsultFilters` extending it with `radius_m`, `search_location_name`, `discovery_filters: dict[str, Any] | None`. Verbatim from `contracts/place_filters.schema.yaml` and data-model.md §§1, 3.
- [X] T005 Migrate `src/kebi/core/recall/types.py::RecallFilters` from `@dataclass` to Pydantic `BaseModel` extending `PlaceFilters` from T004. Fields inherited from the base plus `max_distance_km: float | None`, `created_after: datetime | None`, `created_before: datetime | None`. Update the module docstring to note the migration. Depends on T004.
- [X] T006 [P] Create `tests/core/places/test_filters.py` — round-trip tests for `PlaceFilters`, `RecallFilters`, `ConsultFilters`: (a) construct with all defaults; (b) construct with every field populated; (c) assert `RecallFilters(**kwargs).attributes` walks `cuisine`/`price_hint`/`location_context.city` correctly; (d) assert `ConsultFilters.radius_m` accepts `None`.
- [X] T007 Audit `src/kebi/db/repositories/recall_repository.py::_build_where_clause` — confirm the existing WHERE-clause assembly (feature 027's pulled-forward M4 work) walks `filters.attributes.cuisine`, `filters.attributes.price_hint`, `filters.attributes.location_context.*` correctly against the new Pydantic `RecallFilters`. Update type annotations to reference the Pydantic model. Depends on T005.

**Checkpoint**: Foundation ready — user-story implementation can begin. `poetry run mypy src/` and `poetry run pytest tests/core/places/test_filters.py` both green.

---

## Phase 3: User Story 2 — ConsultService driven by pre-parsed arguments (Priority: P2)

**Goal**: `ConsultService.consult(...)` accepts pre-parsed `query`, pre-built `ConsultFilters`, pre-loaded `saved_places`, optional `preference_context`, and optional `emit` callback. Drops `IntentParser` / memory / taste-main-path. `ConsultResponse.reasoning_steps` field deleted. Services (`RecallService`, `ConsultService`, `ExtractionService`) emit primitive `(step, summary[, duration_ms])` tuples. This story MUST land before US3 — the consult tool wrapper needs this signature to close over.

**Independent Test**: `poetry run pytest tests/core/recall tests/core/consult tests/core/extraction tests/db/repositories/test_recall_repository.py` all pass. The rewritten consult tests spy on `emit` (not `response.reasoning_steps`) and assert the expected `(step, summary)` sequences per branch. `poetry run mypy src/kebi/core/consult/service.py` clean. Flag-off `POST /v1/chat` with a consult intent still returns a `type="consult"` response (legacy chat dispatch branch loads saved places inline).

### Tests for User Story 2 (write first — must FAIL before implementation)

- [X] T008 [P] [US2] Rewrite `tests/core/recall/test_service.py` — update fixtures to use nested-attribute `RecallFilters`. Add a spy `emit` list, pass it via `RecallService.run(..., emit=spy)`, assert the spy receives `("recall.mode", <str>)` followed by `("recall.result", <str>)` across the filter-only and hybrid branches. Do NOT assert on `response.reasoning_steps` (no such field).
- [X] T009 [P] [US2] Rewrite `tests/core/consult/test_service.py` — delete every `response.reasoning_steps` assertion (`test_warming_blend_signal`, `test_active_rejected_chip_filter`, `test_active_confirmed_chips_surfaced`, `test_non_warming_skips_blend`). Replace with a spy `emit` list. Pass pre-built `ConsultFilters` + `saved_places` fixtures; do NOT construct an `IntentParser` or `UserMemoryService`. Assert the expected step-name sequence per branch (geocoded vs not; warming vs active; chip-filter applied vs not). Cover empty `saved_places` (discovery-only) and both-empty (raises `NoMatchesError`).
- [X] T010 [P] [US2] Update `tests/core/extraction/test_service.py` — add a spy `emit` to the existing inline-await tests (from 027 M1). Assert the spy receives `save.parse_input → save.enrich → (optional save.deep_enrichment) → save.validate → save.persist` in order. Parametrize over pipelines where Phase 3 fires vs not; assert `save.deep_enrichment` is emitted iff Phase 3 enrichers ran.
- [X] T011 [P] [US2] Update `tests/db/repositories/test_recall_repository.py` — update test fixtures that construct `RecallFilters(...)` to use the new Pydantic shape. Re-assert WHERE-clause SQL against JSONB paths `attributes->>'cuisine'`, `attributes->>'price_hint'`, `attributes->'location_context'->>'city'`.

### Implementation for User Story 2

- [X] T012 [P] [US2] Edit `src/kebi/core/recall/service.py::RecallService.run` — add `emit: EmitFn | None = None` parameter (positional-keyword, last). Insert `_emit = emit or (lambda _s, _m, _d=None: None)` near the top. Call `_emit("recall.mode", f"mode={mode}; limit={limit}; sort_by={sort_by}")` immediately after the mode is determined. Call `_emit("recall.result", f"{len(results)} places matched")` immediately after the search runs. `RecallResponse` envelope unchanged.
- [X] T013 [P] [US2] Edit `src/kebi/core/extraction/service.py::ExtractionService.run` — add `emit: EmitFn | None = None` parameter (last). Insert `_emit = emit or (lambda _s, _m, _d=None: None)` near the top. Emit `save.parse_input` after input parsing, `save.enrich` after Phase 1, `save.deep_enrichment` only inside the Phase 3 branch (`if phase_3_enrichers_fired: _emit("save.deep_enrichment", ...)`), `save.validate` after Phase 2, `save.persist` after persistence. Envelope shape unchanged from 027 M0.5.
- [X] T014 [P] [US2] Edit `src/kebi/api/schemas/consult.py` — delete the `reasoning_steps: list[ReasoningStep]` field from `ConsultResponse`. Keep the `ReasoningStep` re-export at the top of the file (other importers depend on it). Update the docstring to note that reasoning steps are delivered live via `emit` on the agent path, not bundled into the response.
- [X] T015 [US2] Rewrite `src/kebi/core/consult/service.py::ConsultService.consult(...)` — new signature per `contracts/consult_service_signature.md`: `(user_id, query, saved_places, filters, location=None, preference_context=None, signal_tier="active", emit=None)`. Drop `self._intent_parser`, `self._memory` from `__init__`; drop `self._recall_service`. Delete the `IntentParser.parse(...)` call, the `_memory.load_memories(...)` call, the main-path `_taste_service.get_taste_profile(...)` call, and the internal `_recall_service.run(...)` call. Keep the active-tier chip-filter taste-service read (ADR-061). Delete the internal `reasoning_steps: list[ReasoningStep] = []` list and every `reasoning_steps.append(_consult_step(...))` call — replace each with a single `_emit(step_name, summary)` call using catalog step names (`consult.geocode`, `consult.discover`, `consult.merge`, `consult.dedupe`, `consult.enrich`, `consult.tier_blend`, `consult.chip_filter`). Remove the `_consult_step` helper (dead code). Update the class docstring — replace "6-step pipeline" with "4-phase pipeline: geocode → discover → merge+dedupe → enrich+persist". Depends on T014.
- [X] T016 [US2] Simplify `src/kebi/core/consult/service.py::_persist_recommendation` — drop the `reasoning_steps: list[ReasoningStep]` parameter; drop the `reasoning_steps=...` kwarg on the `ConsultResponse(...)` used to build the JSONB payload. Historical `Recommendation.response` rows with the key remain readable (Pydantic ignores extras by default). Depends on T015.
- [X] T017 [US2] Update `src/kebi/api/deps.py::get_consult_service` — drop `IntentParser()` construction, drop the `memory_service` parameter. `taste_service` injection stays (chip filtering only). Constructor call matches T015's new `__init__`. Type-check with `mypy` before moving on.
- [X] T018 [US2] Update `src/kebi/core/chat/service.py` — in the consult branch of `_dispatch`, load saved places inline via `self._recall.run(query=request.message, user_id=request.user_id, filters=None)` (extract `[r.place for r in response.results]`), build an empty `ConsultFilters()` (acknowledging the flag-off quality regression documented in data-model.md §11), and call `self._consult.consult(user_id=..., query=..., saved_places=..., filters=..., location=..., preference_context=None, signal_tier="active")`. No `emit` on the flag-off path — legacy dispatch does not use reasoning steps. `ChatService.__init__` already holds a `recall_service` reference; no new dep.
- [X] T019 [US2] Run acceptance for US2: `poetry run pytest tests/core/recall tests/core/consult tests/core/extraction tests/db/repositories/test_recall_repository.py && poetry run mypy src/`. All green. Manual check — grep `src/kebi/core/consult/service.py` shows zero references to `IntentParser`, zero to `UserMemoryService`, zero to `format_summary_for_agent`, zero to `_consult_step`, zero to `reasoning_steps`.

**Checkpoint**: US2 complete. The refactored `ConsultService` is independently testable and usable by both the flag-off legacy dispatch and the (soon-to-exist) consult tool wrapper.

---

## Phase 4: User Story 3 — Typed tool wrappers callable by the agent (Priority: P3)

**Goal**: Three `@tool`-decorated async wrappers (`recall_tool`, `save_tool`, `consult_tool`) with Pydantic input schemas that hide `user_id` / `location` / `saved_places`. Shared `_emit.py` helpers with `duration_ms` computation. Agent node extended to emit `agent.tool_decision` steps. Agent prompt updated with the one-tool-call-per-response instruction. Depends on US2 (tools invoke the refactored services).

**Independent Test**: `poetry run pytest tests/core/agent/tools/` all pass. Each tool's `args_schema.model_json_schema()["properties"]` contains only the declared LLM-visible fields (no `user_id`, `location`, `saved_places`). `build_emit_closure("recall")` returns a `(collected, emit)` pair where `emit(step, summary)` with `duration_ms=None` auto-computes from timestamp delta, and `emit(step, summary, duration_ms=123)` uses the supplied value verbatim. `append_summary` carries the total tool-invocation elapsed. Prompt-text grep: `config/prompts/agent.txt` contains the literal "one tool call per response" instruction.

### Tests for User Story 3 (write first — must FAIL before implementation)

- [X] T020 [P] [US3] Create `tests/core/agent/tools/__init__.py` (empty, for pytest discovery).
- [X] T021 [P] [US3] Create `tests/core/agent/tools/test_emit_helpers.py` — cover both helpers: (a) `build_emit_closure("recall")` returns `(collected, emit)`; (b) `emit(step, summary)` with `duration_ms=None` appends a `ReasoningStep` with computed `duration_ms` ≥ 0; (c) `emit(step, summary, duration_ms=42.0)` uses the supplied value verbatim; (d) `append_summary` sets `duration_ms` to total elapsed from first emit to call time; (e) writer-attached branch fans out to `get_stream_writer()` — mock `langgraph.config.get_stream_writer` to return a spy callable; (f) writer-None branch is silent no-op.
- [X] T022 [P] [US3] Create `tests/core/agent/tools/test_recall_tool.py` — mock `RecallService`, assert `RecallToolInput.model_json_schema()["properties"]` keys are exactly `{query, filters, sort_by, limit}` (no `user_id`, `location`). Invoke the tool with a stub `AgentState` injected via the `InjectedState` mechanism; assert `state["user_id"]` / `state["location"]` flowed to `service.run`. Assert the returned `Command.update["last_recall_results"]` is populated. Assert the last `ReasoningStep` in `Command.update["reasoning_steps"]` is `step="tool.summary", visibility="user"`.
- [X] T023 [P] [US3] Create `tests/core/agent/tools/test_save_tool.py` — mock `ExtractionService`, assert `SaveToolInput.model_json_schema()["properties"]` keys are exactly `{raw_input}`. Invoke with envelope outcomes `status="completed"` (saved / duplicate / needs_review) and `status="failed"`; assert `_save_summary(response)` returns the expected narration for each. Save tool does NOT write `last_recall_results`.
- [X] T024 [P] [US3] Create `tests/core/agent/tools/test_consult_tool.py` — mock `ConsultService`, assert `ConsultToolInput.model_json_schema()["properties"]` keys are exactly `{query, filters, preference_context}` (no `saved_places`, `user_id`, `location`). Invoke with `state["last_recall_results"]` populated; assert `service.consult(saved_places=<state value>)` received those places via state. Consult tool does NOT write `last_recall_results`.
- [X] T025 [P] [US3] Create `tests/core/agent/tools/test_tool_summary_narration.py` — parametrized across outcome shapes: `_recall_summary` (hit / miss / filter-mode with and without places); `_save_summary` (saved / duplicate / needs_review / failed / pending); `_consult_summary` (saved+discovered / discovered-only / saved-only / empty). Assert each returns the plan-doc-specified line.

### Implementation for User Story 3

- [X] T026 [US3] Create `src/kebi/core/agent/tools/__init__.py` with `build_tools(recall, extraction, consult) -> list[Tool]` that calls `build_recall_tool(recall)`, `build_save_tool(extraction)`, `build_consult_tool(consult)` in order.
- [X] T027 [US3] Create `src/kebi/core/agent/tools/_emit.py` — `build_emit_closure(tool_name: ToolName) -> (collected, emit)` and `append_summary(collected, tool_name, summary)` per `contracts/tool_schemas.md` + data-model.md. Use `datetime.now(UTC)` + `nonlocal last_ts` for delta-based `duration_ms`; call `langgraph.config.get_stream_writer()` for stream fan-out. Verbatim from research.md item 10 signature block.
- [X] T028 [P] [US3] Create `src/kebi/core/agent/tools/recall_tool.py` — `RecallToolInput` Pydantic model with fields per data-model.md §4 (verbatim `Field(description=...)` text from plan doc). `build_recall_tool(service)` returns an `@tool("recall", args_schema=RecallToolInput)`-decorated async function with signature `(query, filters, sort_by, limit, state: Annotated[AgentState, InjectedState], tool_call_id: Annotated[str, InjectedToolCallId]) -> Command`. Body per `contracts/tool_schemas.md`: `build_emit_closure("recall")` → `service.run(..., emit=emit, limit=limit)` → `append_summary(collected, "recall", _recall_summary(...))` → `Command(update={"last_recall_results": places, "reasoning_steps": prior + collected, "messages": [ToolMessage(...)]})`. Include `_recall_summary(query, filters, places)` + `_filter_noun(filters)` helpers verbatim from plan doc. Depends on T027.
- [X] T029 [P] [US3] Create `src/kebi/core/agent/tools/save_tool.py` — `SaveToolInput` with single `raw_input: str` field. `build_save_tool(service)` returns an `@tool("save", args_schema=SaveToolInput)`-decorated async function. Body: `build_emit_closure("save")` → `service.run(raw_input, state["user_id"], emit=emit)` → `append_summary(collected, "save", _save_summary(response))` → `Command(update={"reasoning_steps": prior + collected, "messages": [ToolMessage(...)]})`. Include `_save_summary(response)` per plan-doc M5 (handles `pending` / `failed` / completed-with-item). Depends on T027.
- [X] T030 [P] [US3] Create `src/kebi/core/agent/tools/consult_tool.py` — `ConsultToolInput` with fields per data-model.md §6 (verbatim `Field(description=...)` text). `build_consult_tool(service)` returns an `@tool("consult", args_schema=ConsultToolInput)`-decorated async function. Body: `build_emit_closure("consult")` → read `saved_places = state.get("last_recall_results") or []` → `service.consult(..., saved_places=saved_places, emit=emit)` → `append_summary(collected, "consult", _consult_summary(response))` → `Command(update={"reasoning_steps": prior + collected, "messages": [ToolMessage(...)]})`. Include `_consult_summary(response)` per plan-doc M5. Consult tool does NOT read or write `last_recall_results` beyond the single state read above. Depends on T027.
- [X] T031 [US3] Extend `src/kebi/core/agent/graph.py::make_agent_node` — inside the returned `agent_node` async closure, after `bound.ainvoke(conversation)` produces `ai_msg`: compute `full_text = (ai_msg.content or "").strip()`; if empty, synthesize from `{"recall": "recall — user referenced saved places", "save": "save — message contains URL or named place", "consult": "consult — recommendation request"}.get(first_tool_call_name, "responding directly")`; call `get_stream_writer()` and forward the full (untruncated) text if a writer is attached; build a `ReasoningStep(step="agent.tool_decision", summary=full_text[:200], source="agent", tool_name=<first tool_call_name or None>, visibility="user", duration_ms=0.0)`; return `{"messages": [ai_msg], "steps_taken": prior+1, "reasoning_steps": (state.get("reasoning_steps") or []) + [step]}`. The existing 027 test for `steps_taken` continues to pass.
- [X] T032 [US3] Edit `config/prompts/agent.txt` — append a new paragraph under the `## Your tools` section reading: "Emit at most one tool call per response. When a request needs more than one tool (for example, recall then consult for a recommendation), chain sequentially across turns — call the first tool, receive its result, then call the next tool in your next response. Do not emit multiple tool calls in a single response." This is the primary mitigation for the last-write-wins race on `AgentState.reasoning_steps` (research.md item 12).
- [X] T033 [US3] Run acceptance for US3: `poetry run pytest tests/core/agent/tools/ && poetry run mypy src/`. All green. Manual check — `grep "one tool call per response" config/prompts/agent.txt` returns a match.

**Checkpoint**: US3 complete. All three tool wrappers are callable via `build_tools(recall, extraction, consult)` and return the expected LLM-visible schemas. Agent node emits `agent.tool_decision` steps. Agent prompt carries the one-tool-call instruction.

---

## Phase 5: User Story 1 — Flag-on conversational turn routed through the agent (Priority: P1)

**Goal**: `POST /v1/chat` honors `config.agent.enabled`. Flag-off = legacy pipeline unchanged. Flag-on = agent graph invoked per turn, returning `ChatResponse(type="agent", ...)` with a filtered user-visible `reasoning_steps` trace. Compiled graph built once at startup via FastAPI `lifespan` → `app.state.agent_graph`. Depends on US2 + US3.

**Independent Test**: `poetry run pytest tests/core/chat tests/api/routes/test_chat.py tests/api/schemas/test_chat.py tests/core/agent/` all pass. Manual: set `agent.enabled: true` in `config/app.yaml`, restart uvicorn, issue the five smoke prompts from quickstart.md step 8 — each returns `type="agent"` with the expected `reasoning_steps` shape. Flip flag off, restart, re-issue — each returns its legacy `type` value.

### Tests for User Story 1 (write first — must FAIL before implementation)

- [X] T034 [P] [US1] Update `tests/api/schemas/test_chat.py` — assert `ChatResponse.type` is a `Literal[...]` with the seven declared values including `"agent"`. Assert `ChatResponse(type="nonsense", ...)` raises `ValidationError`. Assert `ChatResponse(type="agent", message="hi", data={"reasoning_steps": []})` validates.
- [X] T035 [P] [US1] Update `tests/core/chat/test_service.py` — preserve the existing `test_run_legacy_path` tests (flag-off: classify_intent + `_dispatch`). Add `test_run_agent_path`: with `config.agent.enabled=true` and a mocked `agent_graph.ainvoke` returning a final state with one `AIMessage` + a `reasoning_steps` list mixing user + debug visibility, assert `ChatResponse(type="agent", message=<AIMessage.content>, data.reasoning_steps=[<only user-visible>])`. Add `test_run_agent_path_mocks_graph_with_user_id_thread` asserting `graph.ainvoke` is called with `config={"configurable": {"thread_id": user_id}, "metadata": {"user_id": user_id}}`.
- [X] T036 [P] [US1] Create `tests/core/agent/test_agent_graph_chain.py` — build a minimal graph with `GenericFakeChatModel` (research item 11) scripted to emit (1) a recall tool call, (2) a consult tool call, (3) a final content-only `AIMessage`. Use `InMemorySaver`. Mock `RecallService` + `ExtractionService` + `ConsultService`. Invoke the graph; assert `state["last_recall_results"]` is populated after the recall call; assert the captured `AIMessage.tool_calls[1].args` (consult call) does NOT contain a `saved_places` key (SC-009).
- [X] T037 [P] [US1] Create `tests/core/agent/test_recall_reset_between_turns.py` — two `graph.ainvoke` calls on the same `thread_id` using `InMemorySaver`. Turn 1 triggers a recall tool call that populates `last_recall_results` + `reasoning_steps`. Turn 2's `build_turn_payload` resets both; assert before the turn-2 agent node runs, `state["last_recall_results"] is None` and `state["reasoning_steps"] == []`, while `state["messages"]` has accumulated both `HumanMessage`s (via `add_messages` reducer).
- [X] T038 [P] [US1] Create `tests/core/agent/test_reasoning_visibility.py` — run a full recall→consult turn (scripted `GenericFakeChatModel`); assert the final `reasoning_steps` state contains both debug and user entries; assert the JSON payload filtering (`[s for s in state["reasoning_steps"] if s.visibility == "user"]`) contains only the three user-visible step types in the expected order: `agent.tool_decision`, `tool.summary/recall`, `agent.tool_decision`, `tool.summary/consult`.
- [X] T039 [P] [US1] Create `tests/core/agent/test_reasoning_invariants.py` — parametrize across the 8 worked examples in the plan doc's M5 section (Ex 1 standard recommendation, Ex 2 empty recall, Ex 3 pure recall, Ex 4 save success, Ex 5 save duplicate, Ex 6 save+recall chain, Ex 7 direct response, Ex 8 fallback on max_steps; skip Ex 9 max_errors — that fires inside the guard logic which is M9-deferred). For each: assert (1) every turn opens with one `agent.tool_decision`; (2) every tool call produces exactly one user-visible `tool.summary`; (3) `tool_name` set on `tool.summary`, `None` on `agent.tool_decision` and `fallback`; (4) direct-response turns have exactly one user-visible step.
- [X] T040 [P] [US1] Create `tests/core/agent/test_agent_decision_truncation.py` — scripted `AIMessage.content` of 500 chars; assert the agent_node-emitted `ReasoningStep.summary` is ≤ 200 chars; assert when a stream writer is attached, the writer payload contains the full 500 chars (not truncated).
- [X] T041 [P] [US1] Create `tests/core/agent/test_agent_decision_fallback.py` — scripted `AIMessage.content=""` with a `recall` tool call; assert the agent_node-emitted `ReasoningStep.summary` equals `"recall — user referenced saved places"`. Repeat for `save` and `consult` and for no-tool-call-and-no-content (→ `"responding directly"`).
- [X] T042 [P] [US1] Create `tests/api/routes/test_chat_agent.py` — new end-to-end FastAPI test with `agent.enabled=true`. Use dependency override to inject a graph built with `GenericFakeChatModel` + mocked services + `InMemorySaver`. `POST /v1/chat` with `{"user_id":"u","message":"show me my saves"}`; assert response `type="agent"`, `data.reasoning_steps` has the expected shape.

### Implementation for User Story 1

- [X] T043 [US1] Edit `src/kebi/api/schemas/chat.py` — introduce `ChatResponseType = Literal["extract-place", "consult", "recall", "assistant", "clarification", "error", "agent"]`. Change `ChatResponse.type: str` → `ChatResponse.type: ChatResponseType`. Update the docstring to list all seven values.
- [X] T044 [US1] Add `get_agent_graph` dependency in `src/kebi/api/deps.py`: `def get_agent_graph(request: Request) -> Any: return request.app.state.agent_graph`. Import `Request` from `fastapi`.
- [X] T045 [US1] Update `src/kebi/api/deps.py::get_chat_service` — inject `taste_service: TasteModelService = Depends(get_taste_service)`, `config: AppConfig = Depends(get_config)`, `agent_graph: Any = Depends(get_agent_graph)`. Pass all three to the new `ChatService.__init__`.
- [X] T046 [US1] Update `src/kebi/api/main.py` lifespan — inside the existing `@asynccontextmanager` `lifespan(app)`, after the existing startup steps: `checkpointer = await build_checkpointer()`; construct `recall` / `extraction` / `consult` service instances the same way `get_*_service` does (factor into helper functions if cleaner); `tools = build_tools(recall, extraction, consult)`; `llm = get_llm("orchestrator")`; `app.state.agent_graph = build_graph(llm, tools, checkpointer)`. No teardown (research item 5).
- [X] T047 [US1] Rewrite `src/kebi/core/chat/service.py::ChatService` per `contracts/agent_dispatch.md`:
   (a) Update `__init__` to accept `taste_service`, `config: AppConfig`, `agent_graph: Any` in addition to existing deps;
   (b) Rename the existing `run()` body to `_run_legacy(request)`;
   (c) New `run(request)`: outer try/except; branches on `self._config.agent.enabled` → `_run_agent(request)` or `_run_legacy(request)`;
   (d) Implement `_run_agent(request)`: calls new helpers `_compose_taste_summary(user_id)` + `_compose_memory_summary(user_id)`; calls `build_turn_payload(...)`; calls `graph.ainvoke(payload, config={"configurable": {"thread_id": request.user_id}, "metadata": {"user_id": request.user_id}})`; extracts last `AIMessage`; filters `reasoning_steps` by `visibility == "user"`; returns `ChatResponse(type="agent", message=<ai_message.content or "">, data={"reasoning_steps": [s.model_dump(mode="json") for s in user_steps]})`;
   (e) Add `_compose_taste_summary(user_id) -> str` (wraps `TasteModelService.get_taste_profile` + `format_summary_for_agent`; returns `""` on None profile);
   (f) Add `_compose_memory_summary(user_id) -> str` (wraps `UserMemoryService.load_memories` + `"\n".join`; returns `""` on empty);
   (g) Add `_last_ai_message(messages)` helper iterating `reversed(messages)`.
   Depends on T043, T044, T045, T046.
- [X] T048 [US1] Run acceptance for US1: `poetry run pytest tests/core/chat tests/api/routes/test_chat.py tests/api/schemas/test_chat.py tests/core/agent/ && poetry run mypy src/`. All green.

**Checkpoint**: All three user stories complete. Feature is feature-flag-gated end-to-end; flag-off preserves legacy behavior exactly; flag-on routes every chat request through the agent.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Docs, Bruno examples, full-suite verification, and manual smoke walkthrough per quickstart.md. No feature logic lands here.

- [X] T049 [P] Update `docs/api-contract.md` — add a new subsection documenting `ChatResponse.type="agent"`: response shape (`message` = final AI reply, `data.reasoning_steps` = filtered user-visible entries); the three user-visible step types (`agent.tool_decision`, `tool.summary`, `fallback`); the `duration_ms` field on each step. Note the flag-off default — existing consumers observe no change until M10. Verbatim alignment with `contracts/chat_response_agent.openapi.yaml`.
- [X] T050 [P] Add `kebi-config/bruno/chat_agent_example.bru` — sample `POST /v1/chat` request + example agent-path response (flag-on) showing the new type and `data.reasoning_steps` shape. Mirrors existing `.bru` files' conventions. External repo path; document the expected location in the PR description if the repo is not checked out locally.
- [X] T051 Run full lint + format + type check: `poetry run ruff check src/ tests/ && poetry run ruff format --check src/ tests/ && poetry run mypy src/`. All three must exit 0.
- [X] T052 Run full test suite: `poetry run pytest -x`. Flag-off is the shipped default; the full suite must be green with zero regression vs the T001 baseline count (SC-001).
- [ ] T053 Manual flag-off smoke per `quickstart.md` step 7 — start uvicorn with `agent.enabled: false`, issue three `curl` requests (recall / consult / save intents), confirm all three return their respective legacy `type` values (never `"agent"`).
- [ ] T054 Manual flag-on smoke per `quickstart.md` step 8 — flip `config/app.yaml` to `agent.enabled: true`, restart uvicorn, issue the five reference `curl` requests (pure recall, recommendation, save-from-URL, direct Q&A, two-turn same-user). Confirm each returns `type="agent"` with `data.reasoning_steps` matching the expected shape (Ex 1–Ex 7 from the plan doc's worked examples). Revert to `agent.enabled: false` before committing.
- [ ] T055 Manual prompt + one-tool-call verification per `quickstart.md` step 8a — grep `config/prompts/agent.txt` for the literal "one tool call per response" string. During the flag-on smoke (T054), open the Langfuse trace for the recommendation-request turn and confirm every `AIMessage` span has at most one `tool_calls` entry (the M9 automated test will guard this long-term; this is the manual canary until then).
- [ ] T056 Manual Langfuse trace review per `quickstart.md` step 9 — for `user_id=u-agent-smoke`, verify one outer trace per turn contains (a) one orchestrator LLM span per `agent_node` run, (b) one tool span per tool call, (c) `user_id` as a trace attribute, (d) no orphan spans. Manual validation for FR-032 + SC-010.

**Checkpoint**: Feature 028 is merge-ready. Artifacts committed on branch `028-agent-tools-wiring`; push + PR is a separate, manually-initiated step.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)** — no dependencies. T001 runs first.
- **Phase 2 (Foundational)** — depends on Setup. Blocks US1, US2, US3.
- **Phase 3 (US2)** — depends on Foundational (T002, T003, T004, T005).
- **Phase 4 (US3)** — depends on Foundational + US2 (tools import the refactored services; consult tool closes over the new `ConsultService.consult` signature).
- **Phase 5 (US1)** — depends on Foundational + US2 + US3 (chat service invokes the graph which invokes the tools which invoke the services; main.py lifespan needs `build_tools` to exist).
- **Phase 6 (Polish)** — depends on US1 complete.

### User Story Dependencies

- **US2 (P2)** — blocks US3. `ConsultService`'s new signature must exist before `consult_tool` closes over it.
- **US3 (P3)** — blocks US1. `build_tools(...)` must exist before `api/main.py` lifespan can compile the graph.
- **US1 (P1)** — terminal. Nothing depends on it within this feature.

Note: the priority ordering (P1 > P2 > P3) reflects operator-visible value delivered; the execution order is the reverse (P2 → P3 → P1) because of the dependency chain. This is a conscious spec-kit trade-off — US1 is "the visible outcome" even though it lands last.

### Within Each User Story

- Tests (T008–T011 for US2; T020–T025 for US3; T034–T042 for US1) are written first and FAIL until the matching implementation lands.
- For US2: schema edit (T014) before service rewrite (T015, T016), then deps-wiring (T017) and dispatch (T018).
- For US3: `_emit.py` (T027) before tool wrappers (T028–T030) because they import the helpers. Tool module init (T026) can be stubbed first or landed with T027.
- For US1: schema tightening (T043) and dependency wiring (T044, T045, T046) before the chat-service rewrite (T047).

### Parallel Opportunities

**Phase 2 (Foundational)** — three tasks run in parallel:
- T002 (core/emit.py), T003 (reasoning.py duration_ms), T004 (places/filters.py).
- T005 (recall/types.py migration) is sequential on T004.
- T006 (test_filters.py) is parallel with T007 (repo audit).

**US2 tests** — T008, T009, T010, T011 all different files → all `[P]`.

**US2 implementation** — T012, T013, T014 all different files → all `[P]`. T015 sequences on T014 (needs schema field removed first). T016 on T015 (same file). T017 and T018 each sequential on prior state but parallel to each other (different files).

**US3 tests** — T020 through T025 all different files → all `[P]`.

**US3 implementation** — T028, T029, T030 all different files after T027 lands → all `[P]`.

**US1 tests** — T034 through T042 all different files → all `[P]`.

**US1 implementation** — mostly sequential by file dependency.

**Polish** — T049, T050 parallel (different files); T051, T052, T053, T054, T055, T056 sequential (each depends on preceding manual state).

---

## Parallel Example: Phase 2 Foundational

```bash
# Launch in parallel:
Task T002 — Create src/kebi/core/emit.py
Task T003 — Edit src/kebi/core/agent/reasoning.py (add duration_ms)
Task T004 — Create src/kebi/core/places/filters.py

# Then sequentially:
Task T005 — Migrate src/kebi/core/recall/types.py (extends PlaceFilters from T004)

# Then in parallel:
Task T006 — Create tests/core/places/test_filters.py
Task T007 — Audit src/kebi/db/repositories/recall_repository.py
```

## Parallel Example: US3 Implementation after T027

```bash
# After T027 (_emit.py) lands, three tool wrappers can be built in parallel:
Task T028 — Create src/kebi/core/agent/tools/recall_tool.py
Task T029 — Create src/kebi/core/agent/tools/save_tool.py
Task T030 — Create src/kebi/core/agent/tools/consult_tool.py

# T031 and T032 edit different files and can run in parallel with T028–T030:
Task T031 — Extend src/kebi/core/agent/graph.py::make_agent_node
Task T032 — Edit config/prompts/agent.txt
```

---

## Implementation Strategy

### Dependency-Respecting Order (the only viable order for this feature)

1. **Setup + Foundational** → foundation ready (T001–T007).
2. **US2** (M4 — ConsultService refactor + emit pattern) → internal refactor complete. The flag-off legacy path still works; `RecallService` / `ExtractionService` now emit primitives (T008–T019).
3. **US3** (M5 — tool wrappers + agent-node `agent.tool_decision` + prompt edit) → tools callable in isolation; agent prompt carries the one-tool-call instruction (T020–T033).
4. **US1** (M6 — flag fork + lifespan + response type) → end-to-end flag-on path works; flag-off path unchanged (T034–T048).
5. **Polish** → docs, Bruno, full-suite verification, manual smoke (T049–T056).

### Incremental Delivery

Each phase is commit-worthy on its own:

- **Commit 1** — Foundational: `feat(agent): M4 foundation — EmitFn Protocol, ReasoningStep.duration_ms, PlaceFilters family`.
- **Commit 2** — US2: `refactor(consult): drop IntentParser + memory + main-path taste-load; emit pattern; delete ConsultResponse.reasoning_steps`.
- **Commit 3** — US3: `feat(agent): M5 tool wrappers with shared _emit.py helpers + one-tool-call prompt instruction`.
- **Commit 4** — US1: `feat(chat): M6 flag-fork agent path behind config.agent.enabled`.
- **Commit 5** — Polish: `docs(api): document agent-path ChatResponse + Bruno example`.

### Parallel Team Strategy (not applicable here)

This feature has a strict US2 → US3 → US1 chain. Parallel work WITHIN a story is possible (the `[P]` markers indicate where), but parallel work ACROSS stories is not — US3 can't start until US2's `ConsultService` signature is finalized, and US1 can't start until US3's `build_tools()` exists. Single-developer serial execution is the expected mode.

---

## Notes

- [P] tasks = different files, no dependencies on incomplete tasks in the same phase.
- [Story] label (US1 / US2 / US3) maps every user-story-phase task to a specific story for traceability.
- Tests are written first within each story per FR-035; they are expected to FAIL until the implementation lands.
- Commit after each user story completes (at its checkpoint) for clean `git log` + easy rollback.
- Flag defaults to OFF in the shipped `config/app.yaml` — flipping it to on is out of scope for this feature (M10).
- The only external contract change is the additive `"agent"` value on `ChatResponse.type`. Flag-off default means product-repo consumers observe no change until a later feature.
- SSE endpoint (M7), NodeInterrupt (M8), per-tool timeouts (M9), flag flip (M10), legacy pipeline deletion (M11) are all deferred and explicitly out of scope.
