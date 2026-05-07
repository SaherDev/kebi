---
description: "Task list for feature 027-agent-foundation"
---

# Tasks: Agent Foundation (M0.5 + M1 + M2 + M3)

**Input**: Design documents from `/Users/saher/dev/repos/kebi-dev/kebi/specs/027-agent-foundation/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: Included — this feature's Success Criteria (SC-001 through SC-013) are defined in test-verifiable form and the spec's "Independent Test" paragraph for each user story is test-based. Every user story gets test tasks written before the implementation tasks that satisfy them.

**Organization**: Tasks are grouped by user story. US1 (M0.5) and US2 (M1) are the P1 stories; US3 (M2) and US4 (M3) are P2. US1 → US2 is sequential (M1 assumes the new schema shape). US3 → US4 is sequential (M3's graph reads `AgentConfig`). US1/US2 and US3/US4 are independent of each other and can run in parallel on separate developer streams.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no in-phase dependencies)
- **[Story]**: Which user story this task belongs to (US1 / US2 / US3 / US4)
- Include exact file paths

## Path Conventions

Single-project src layout (ADR-001). All paths absolute or repo-rooted. Tests mirror `src/` structure under `tests/` (ADR-004).

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Add the one new dependency the feature introduces. Everything else uses existing infrastructure.

- [X] T001 Add `langgraph-checkpoint-postgres` to `/Users/saher/dev/repos/kebi-dev/kebi/pyproject.toml` via `poetry add langgraph-checkpoint-postgres`. Record the exact installed version in `specs/027-agent-foundation/research.md` post-implement addendum (line "Exact pinned version"). Verify the import by running `poetry run python -c "from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver; print('ok')"`. **Done**: `langgraph-checkpoint-postgres ^3.0.5` + `psycopg[binary] ^3.3.3` installed (binary wrapper required for libpq at import-time). Verified import successful.
- [X] T002 [P] Confirm baseline still green before starting: run `poetry run ruff check src/ tests/`, `poetry run ruff format --check src/ tests/`, `poetry run mypy src/`, `poetry run pytest -x`. Capture the baseline counts so regressions are obvious during `/speckit.implement` execution. **Baseline captured**: ruff=5 errors (1 src UP038, 4 tests E501), format=8 files, mypy=1 error (taste/service.py:58 lambda), pytest=490 tests collected. Pre-existing; not introduced by this feature.

**Checkpoint**: new dependency importable; baseline clean.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Minimal cross-story prerequisites. No shared model scaffolding required — each story owns its own entities. The only blocking work is the ADR placeholder (touched by US1) being reserved so two parallel story streams don't collide on `docs/decisions.md`.

- [X] T003 Reserve the ADR-063 heading in `/Users/saher/dev/repos/kebi-dev/kebi/docs/decisions.md` by appending a skeleton entry `## ADR-063: Two-level ExtractPlaceResponse status + raw_input rename (2026-04-21)` with `TBD` placeholders for Context / Decision / Consequences. US1 fills it in; US3/US4 work may also add ADR entries later and this reservation prevents merge conflicts.

**Checkpoint**: foundation ready — US1/US2 and US3/US4 can proceed on independent streams.

---

## Phase 3: User Story 1 — Cleaner extraction response contract (Priority: P1) 🎯 MVP

**Goal**: Rewrite `ExtractPlaceResponse` / `ExtractPlaceItem` to separate pipeline-level and per-place status, rename `source_url → raw_input` (verbatim), bump Redis prefix to `extraction:v2:`, land ADR-063, update docs and Bruno collection.

**Independent Test**: `poetry run pytest tests/api/schemas/test_extract_place.py tests/api/routes/test_extraction.py` — new envelope shape covered end-to-end: status enum on envelope, non-null `place`/`confidence` on items, `raw_input` byte-identical to input, 404 on legacy keys, Redis writes under `extraction:v2:` prefix.

### Tests for User Story 1 ⚠️ write first, verify they FAIL before implementing

- [X] T004 [P] [US1] Rewrite `/Users/saher/dev/repos/kebi-dev/kebi/tests/api/schemas/test_extract_place.py` to cover the v2 envelope: envelope `status` enum values, `results` empty iff `status != "completed"`, `raw_input` preserves verbatim input, `ExtractPlaceItem.place`/`confidence` required non-null, `status` Literal restricted to `{saved, needs_review, duplicate}`, model_validator rejects `status=completed` with empty results and `status in {pending, failed}` with non-empty results. Delete any asserts constructing `ExtractPlaceItem(place=None, ...)`.
- [X] T005 [P] [US1] Update `/Users/saher/dev/repos/kebi-dev/kebi/tests/api/routes/test_extraction.py` so polling-route test cases (a) return the new envelope shape on success, (b) assert 404 path unchanged for missing keys, (c) confirm the written payload under `extraction:v2:{request_id}` matches the new envelope on the happy path (or add a fixture checking the repository wrote under that prefix).

### Implementation for User Story 1

- [X] T006 [US1] Rewrite `/Users/saher/dev/repos/kebi-dev/kebi/src/kebi/api/schemas/extract_place.py` per `specs/027-agent-foundation/data-model.md` §1–2: `ExtractPlaceResponse(status: Literal["pending","completed","failed"], results: list[ExtractPlaceItem], raw_input: str | None, request_id: str | None)` with envelope `@model_validator` enforcing the emptiness invariant; `ExtractPlaceItem(place: PlaceObject, confidence: float, status: Literal["saved","needs_review","duplicate"])` with `@field_validator` on confidence range `[0.0, 1.0]`. Drop the old `source_url` field entirely.
- [X] T007 [US1] Update `/Users/saher/dev/repos/kebi-dev/kebi/src/kebi/core/extraction/service.py` so the response-construction paths in both `run()` and `_run_background()` emit the new shape: replace the old `_outcome_to_dict` with `_outcome_to_item_dict` (maps only `saved`/`needs_review`/`duplicate` to item dicts) and add an `_is_real(outcome)` predicate that filters `below_threshold` outcomes out of `results`. Pipeline-level `pending`/`failed` live only on the envelope. **Keep the existing `asyncio.create_task` scaffolding intact** — the inline-await refactor is US2, not US1. US1 only reshapes the payloads.
- [X] T008 [US1] Update `/Users/saher/dev/repos/kebi-dev/kebi/src/kebi/core/chat/service.py::_dispatch` extract-place branch so its detection switches from `any(r.status == "pending" for r in extract_result.results)` to `extract_result.status == "pending"` (envelope-level). Update the message-composition reads so `saved` / `needs_review` / `duplicates` loops reference the new shape.
- [X] T009 [P] [US1] Bump the Redis key prefix in `/Users/saher/dev/repos/kebi-dev/kebi/src/kebi/core/extraction/status_repository.py`: change module constant `_KEY_PREFIX = "extraction"` → `_KEY_PREFIX = "extraction:v2"`. No compatibility read path — legacy keys are intentionally unread per clarification. Add a one-line comment noting ADR-063.
- [X] T010 [P] [US1] Fill in ADR-063 in `/Users/saher/dev/repos/kebi-dev/kebi/docs/decisions.md` (the heading reserved by T003): Context = null-placeholder smell + multi-outcome extractions conflating pipeline with per-place state; Decision = pipeline status on envelope, per-place status on items, `source_url` renamed to `raw_input` as verbatim echo, Redis prefix bumped to `extraction:v2:`; Consequences = contract break requiring product-repo sync (FR-036), cleaner schema downstream, 404-on-legacy-keys rollout per clarification.
- [X] T011 [P] [US1] Update `/Users/saher/dev/repos/kebi-dev/kebi/docs/api-contract.md` extract-place section to reflect the new envelope shape: move `status` to the response envelope, drop `| null` from `place`/`confidence` in the item field table, remove `"pending"` / `"failed"` from the per-item status Literal, rename `source_url` → `raw_input` with a verbatim-echo note, mention the `extraction:v2:` Redis prefix as operational detail.
- [X] T012 [P] [US1] Update the Bruno collection at `/Users/saher/dev/repos/kebi-dev/kebi/kebi-config/bruno/` — every `.bru` file whose example response touched `extract-place` or `/v1/extraction/{request_id}` gets its example JSON rewritten to the new envelope (status on envelope, non-null items, `raw_input`). Per FR-008.
- [X] T013 [US1] Run the US1 test slice: `poetry run pytest tests/api/schemas/test_extract_place.py tests/api/routes/test_extraction.py tests/core/extraction/test_service.py tests/core/chat/test_service.py -v` — all green. Then `poetry run mypy src/` — clean (catches any stray `ExtractPlaceItem(place=None,...)` in other call sites).

**Checkpoint**: US1 complete and deployable. Product-repo coordination (FR-036) can now happen in parallel with US2–US4 work.

---

## Phase 4: User Story 2 — Save tool can observe the real extraction outcome inline (Priority: P1)

**Goal**: Inline-await the extraction pipeline inside `ExtractionService.run()`. Move the `asyncio.create_task` fire-and-return to `ChatService._dispatch_extraction`. Pass the route-generated `request_id` through to `run()` so both the envelope and the Redis write share one id.

**Depends on**: US1 (new envelope shape is assumed by the rewritten tests).

**Independent Test**: `poetry run pytest tests/core/extraction/test_service.py tests/core/chat/test_service.py` — `ExtractionService.run()` awaited in a unit test returns `status ∈ {completed, failed}` (never `pending`); `_dispatch_extraction` still returns `status="pending"` + `request_id` synchronously while a background task writes the real envelope to Redis under `extraction:v2:{request_id}`; `raw_input` byte-identical to input on both paths.

### Tests for User Story 2 ⚠️ write first, verify they FAIL before implementing

- [X] T014 [P] [US2] Rewrite `/Users/saher/dev/repos/kebi-dev/kebi/tests/core/extraction/test_service.py`: `test_run_returns_terminal_envelope_inline` (pipeline → `status="completed"`, `results` non-empty); `test_run_pipeline_empty_returns_failed` (pipeline → `None` → `status="failed"`, `results=[]`); `test_run_all_below_threshold_returns_failed` (every outcome is `below_threshold` → `status="failed"`, `results=[]`, no null items); `test_run_mixed_above_and_below_threshold_filters_below` (above-threshold only in `results`, envelope `status="completed"`); `test_run_writes_redis_under_v2_prefix` (status repo receives a write keyed `extraction:v2:{request_id}` with the returned envelope). **Delete** `test_run_fires_background_task`.
- [X] T015 [P] [US2] Add to `/Users/saher/dev/repos/kebi-dev/kebi/tests/core/chat/test_service.py`: `test_dispatch_extraction_returns_pending_synchronously` (mocks `ExtractionService.run` to `await asyncio.sleep(10)`; asserts `_dispatch` completes in <2s with `data.status="pending"`); `test_dispatch_extraction_background_writes_real_envelope` (mock returns completed envelope; assert status repo wrote under the route-owned `request_id`); `test_dispatch_extraction_raw_input_is_verbatim` (submit `"  https://tiktok.com/...?utm=spam  "` with leading whitespace and a tracking param; assert `data.raw_input` is byte-identical to input).

### Implementation for User Story 2

- [X] T016 [US2] Rewrite `ExtractionService.run()` in `/Users/saher/dev/repos/kebi-dev/kebi/src/kebi/core/extraction/service.py` per the plan's M1 target shape: remove the `asyncio.create_task` call at line 74; inline the pipeline body; return an `ExtractPlaceResponse` with envelope-level `status ∈ {completed, failed}` and non-empty `results` only when completed. Also widen the signature to accept an optional `request_id: str | None = None` argument so callers can inject the id used for the Redis write (see `contracts/chat_extract_dispatch.md`). Fire the Redis write from `run()` itself (FR-010); delete `_run_background`.
- [X] T017 [US2] Update `/Users/saher/dev/repos/kebi-dev/kebi/src/kebi/core/chat/service.py::_dispatch` extract-place branch: generate `request_id = uuid4().hex` at the route layer; wrap `self._extraction.run(request.message, request.user_id, request_id=request_id)` in `asyncio.create_task(...)`; return `ChatResponse(type="extract-place", message="On it — extracting the place in the background. Check back in a moment.", data=ExtractPlaceResponse(status="pending", results=[], raw_input=request.message, request_id=request_id).model_dump(mode="json"))` immediately. Log exceptions in the background via `logger.exception` (no Redis write on failure — same behavior as the deleted `_run_background`).
- [X] T018 [US2] Run the US2 test slice: `poetry run pytest tests/core/extraction tests/core/chat -v` — green. Then `poetry run mypy src/` — clean.

**Checkpoint**: US1 + US2 complete. External `/v1/chat` behavior unchanged; internal inline-await unblocks the M5 Save tool.

---

## Phase 5: User Story 3 — Agent configuration and prompt are externalized and togglable (Priority: P2)

**Goal**: Add typed `AgentConfig` + `ToolTimeoutsConfig` to `AppConfig`. Add `agent:` block to `config/app.yaml`. Ship `config/prompts/agent.txt` per the template contract. Extend `_load_prompts` with an eager slot-validation pass (FR-018a).

**Depends on**: nothing in US1/US2 — runs on a parallel stream.

**Independent Test**: `poetry run pytest tests/core/config/` + the one-liner `poetry run python -c "from kebi.core.config import get_config; c = get_config(); print(c.agent.enabled, c.agent.max_steps, c.prompts['agent'].file)"` prints `False 10 agent.txt`; removing the `{memory_summary}` slot from a fixture prompt aborts `get_config()` with a `ValueError` naming the missing slot.

### Tests for User Story 3 ⚠️ write first, verify they FAIL before implementing

- [X] T019 [P] [US3] Create `/Users/saher/dev/repos/kebi-dev/kebi/tests/core/config/test_config.py` (new directory + `__init__.py` if absent): `test_agent_config_defaults` (asserts `enabled=False`, `max_steps=10`, `max_errors=3`, `checkpointer_ttl_seconds=86400`, `tool_timeouts_seconds.recall=5 consult=10 save=25`); `test_agent_config_rejects_zero_max_steps` (uses a `tmp_path` YAML fixture with `max_steps: 0` → expect `ValidationError`); `test_agent_prompt_loads_with_both_slots` (reads `get_config().prompts['agent'].content`, asserts `{taste_profile_summary}` and `{memory_summary}` both present); `test_agent_prompt_missing_slot_aborts_boot` (monkeypatches `find_project_root()` to a `tmp_path` with a stripped-down `agent.txt` missing `{memory_summary}` → expect `ValueError`); `test_agent_prompt_covers_places_range` (content contains `restaurant`, `museum`, `hotel` — regression guard against food-only drift).

### Implementation for User Story 3

- [X] T020 [P] [US3] Extend `/Users/saher/dev/repos/kebi-dev/kebi/src/kebi/core/config.py`: add `ToolTimeoutsConfig(BaseModel)` with `recall: int = 5`, `consult: int = 10`, `save: int = 25` and a `model_validator` enforcing `>= 1`. Add `AgentConfig(BaseModel)` with `enabled: bool = False`, `max_steps: int = 10`, `max_errors: int = 3`, `checkpointer_ttl_seconds: int = 86400`, `tool_timeouts_seconds: ToolTimeoutsConfig = ToolTimeoutsConfig()` and a `model_validator` enforcing each int field `>= 1`. Attach to `AppConfig` as `agent: AgentConfig = AgentConfig()`. Extend `_load_prompts(raw)` with an internal `required_slots: dict[str, list[str]] = {"agent": ["{taste_profile_summary}", "{memory_summary}"]}` and after reading each prompt's content, assert every required slot appears — raise `ValueError(f"Prompt {name!r} ({path}) is missing required template slot {slot!r}")` on miss.
- [X] T021 [P] [US3] Add the `agent:` block to `/Users/saher/dev/repos/kebi-dev/kebi/config/app.yaml` per `specs/027-agent-foundation/contracts/agent_config.schema.yaml`: `enabled: false`, `max_steps: 10`, `max_errors: 3`, `checkpointer_ttl_seconds: 86400`, `tool_timeouts_seconds: {recall: 5, consult: 10, save: 25}`. Register the new prompt in the existing `prompts:` block: add `agent: agent.txt` alongside `taste_regen: taste_regen.txt`.
- [X] T022 [P] [US3] Create `/Users/saher/dev/repos/kebi-dev/kebi/config/prompts/agent.txt` per `specs/027-agent-foundation/contracts/agent_prompt.template.md`: places-advisor persona covering full `PlaceType` range (restaurants, bars, cafes, museums, shops, hotels, services); high-level three-tool guidance (recall/save/consult — no per-arg shaping); both template slots `{taste_profile_summary}` and `{memory_summary}` embedded in a context section; ADR-044 safety block with defensive-instruction clause, `<context>` tag discipline, and Instructor-validation reference. Forbidden: hardcoded names, per-tool arg rules, model-name references.
- [X] T023 [US3] Run the US3 test slice: `poetry run pytest tests/core/config -v`; `poetry run python -c "from kebi.core.config import get_config; c = get_config(); print(c.agent.enabled, c.prompts['agent'].file)"` → prints `False agent.txt`; `poetry run mypy src/` — clean.

**Checkpoint**: US3 complete. `AgentConfig` readable from `get_config().agent`; prompt validated at boot; no caller reads the new config yet (wiring lands in US4 for `max_steps`/`max_errors`, in M5/M9 for timeouts).

---

## Phase 6: User Story 4 — Agent graph skeleton compiles and routes correctly (Priority: P2)

**Goal**: Create `core/agent/` module. Ship `AgentState` TypedDict, `build_turn_payload` helper (transient-field reset), `ReasoningStep` Pydantic model (re-exported by `api/schemas/consult.py`), `build_graph` factory with `agent`/`tools`/`fallback` nodes and `should_continue` router, `build_checkpointer` backed by `AsyncPostgresSaver`, and Alembic's `include_object` exclusion filter.

**Depends on**: US3 (reads `AgentConfig.max_steps` / `max_errors` in `should_continue`).

**Independent Test**: `poetry run pytest tests/core/agent -v` — graph compiles with `InMemorySaver` + mocked LLM; `should_continue` routes correctly across all four branches; `build_turn_payload` resets transient fields; `fallback_node` emits exactly one user-visible `ReasoningStep`; `AsyncPostgresSaver.setup()` idempotent against docker-compose Postgres; `poetry run alembic check` passes.

### Tests for User Story 4 ⚠️ write first, verify they FAIL before implementing

- [X] T024 [P] [US4] Create `/Users/saher/dev/repos/kebi-dev/kebi/tests/core/agent/__init__.py` (empty) and `/Users/saher/dev/repos/kebi-dev/kebi/tests/core/agent/conftest.py` exposing `checkpointer` fixture returning `InMemorySaver()` from `langgraph.checkpoint.memory`, plus a `mock_llm` fixture returning a fake chat model whose `.ainvoke(...)` returns a preconfigured `AIMessage` and whose `.bind_tools(...)` returns itself.
- [X] T025 [P] [US4] Create `/Users/saher/dev/repos/kebi-dev/kebi/tests/core/agent/test_reasoning.py`: `test_reasoning_step_user_defaults` (visibility defaults to "user", timestamp auto-set); `test_reasoning_step_tool_requires_tool_name` (source="tool" without tool_name → `ValidationError`); `test_reasoning_step_non_tool_forbids_tool_name` (source="fallback" with tool_name="recall" → `ValidationError`); `test_consult_reasoning_step_reexport` (imports `ReasoningStep` from `kebi.api.schemas.consult` and confirms it is the same class as `kebi.core.agent.reasoning.ReasoningStep`).
- [X] T026 [P] [US4] Create `/Users/saher/dev/repos/kebi-dev/kebi/tests/core/agent/test_state.py`: `test_agent_state_typed_dict_shape` (smoke construction with all fields); `test_add_messages_reducer_appends` (build `StateGraph(AgentState)` with a passthrough node + `InMemorySaver`, invoke twice on same thread_id with different `HumanMessage`s, assert `messages` accumulates both + passthrough responses).
- [X] T027 [P] [US4] Create `/Users/saher/dev/repos/kebi-dev/kebi/tests/core/agent/test_invocation.py`: `test_build_turn_payload_resets_transient_fields` (call twice with different inputs, assert `last_recall_results is None` and `reasoning_steps == []` on both calls, `steps_taken=0`, `error_count=0`); `test_build_turn_payload_appends_human_message` (asserts the `messages` entry is exactly one `HumanMessage(content=message)`); `test_build_turn_payload_preserves_location_and_summaries` (non-transient fields pass through verbatim).
- [X] T028 [P] [US4] Create `/Users/saher/dev/repos/kebi-dev/kebi/tests/core/agent/test_graph_routing.py`: parametric unit tests over `should_continue`: `test_routes_to_fallback_on_max_errors` (state with `error_count >= max_errors` → "fallback"); `test_routes_to_fallback_on_max_steps`; `test_routes_to_tools_when_last_ai_has_tool_calls`; `test_routes_to_end_when_last_ai_no_tool_calls`. Use `get_config()` or monkeypatch `max_steps`/`max_errors` as needed — tests are pure; no LLM.
- [X] T029 [P] [US4] Create `/Users/saher/dev/repos/kebi-dev/kebi/tests/core/agent/test_fallback.py`: `test_fallback_node_appends_user_visible_step` (state with `steps_taken >= max_steps`: assert `reasoning_steps[-1]` has `step="fallback"`, `source="fallback"`, `tool_name is None`, `visibility="user"`, summary contains the step count); `test_fallback_node_composes_ai_message` (asserts the returned state update's `messages` contains exactly one `AIMessage` with the graceful text); `test_fallback_node_no_debug_diagnostics_in_m3` (no `max_steps_detail` or `max_errors_detail` entries anywhere — deferred to M9).
- [X] T030 [P] [US4] Create `/Users/saher/dev/repos/kebi-dev/kebi/tests/core/agent/test_agent_node.py`: uses the `mock_llm` fixture. `test_agent_node_binds_tools` (assert `mock_llm.bind_tools` was called with the tools list); `test_agent_node_renders_prompt_with_both_slots_substituted` (assert the SystemMessage content sent to llm has real values in place of both template slots — verify the `{taste_profile_summary}` / `{memory_summary}` literals are gone); `test_agent_node_increments_steps_taken`; `test_agent_node_appends_ai_message`.
- [X] T031 [US4] Create `/Users/saher/dev/repos/kebi-dev/kebi/tests/core/agent/test_graph_compile.py`: `test_build_graph_compiles_with_mock_llm_and_inmemorysaver` (assert returned graph has `entry_point == "agent"` and nodes include `{"agent", "tools", "fallback"}`).
- [X] T032 [US4] Create `/Users/saher/dev/repos/kebi-dev/kebi/tests/core/agent/test_checkpointer.py`: integration-style test marked with a skip-if-no-postgres guard — `test_setup_is_idempotent` (awaits `build_checkpointer()` twice against docker-compose Postgres, asserts second call does not raise, asserts `checkpoints`/`checkpoint_blobs`/`checkpoint_writes` tables exist via an async connection check).
- [X] T033 [US4] Create `/Users/saher/dev/repos/kebi-dev/kebi/tests/alembic/test_checkpointer_exclusion.py` (or extend existing Alembic tests): `test_alembic_check_excludes_checkpointer_tables` — runs `poetry run alembic check` via subprocess against the local docker-compose Postgres after `build_checkpointer` has been called once; asserts neither `DROP TABLE checkpoints` nor any reference to the three library tables appears in the output.

### Implementation for User Story 4

- [X] T034 [P] [US4] Create `/Users/saher/dev/repos/kebi-dev/kebi/src/kebi/core/agent/__init__.py` (empty module marker).
- [X] T035 [P] [US4] Create `/Users/saher/dev/repos/kebi-dev/kebi/src/kebi/core/agent/reasoning.py` with the `ReasoningStep` Pydantic model per `data-model.md` §6: fields `step`, `summary`, `source: Literal["tool","agent","fallback"]`, `tool_name: Literal["recall","save","consult"] | None = None`, `visibility: Literal["user","debug"] = "user"`, `timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))`. Add `@model_validator(mode="after")` enforcing tool_name iff source="tool".
- [X] T036 [P] [US4] Edit `/Users/saher/dev/repos/kebi-dev/kebi/src/kebi/api/schemas/consult.py` to replace the existing minimal `ReasoningStep` with a re-export: `from kebi.core.agent.reasoning import ReasoningStep  # noqa: F401` (FR-024). Update any imports inside `consult.py` that referenced the local shape. `ConsultResponse.reasoning_steps: list[ReasoningStep]` now adopts the richer schema.
- [X] T037 [P] [US4] Create `/Users/saher/dev/repos/kebi-dev/kebi/src/kebi/core/agent/state.py` with `AgentState(TypedDict)` per `data-model.md` §5: `messages: Annotated[list[BaseMessage], add_messages]`, `taste_profile_summary: str`, `memory_summary: str`, `user_id: str`, `location: dict | None`, `last_recall_results: list[PlaceObject] | None`, `reasoning_steps: list[ReasoningStep]`, `steps_taken: int`, `error_count: int`. Imports from `langgraph.graph.message.add_messages`, `langchain_core.messages.BaseMessage`, `kebi.core.places.models.PlaceObject`, `kebi.core.agent.reasoning.ReasoningStep`.
- [X] T038 [US4] Create `/Users/saher/dev/repos/kebi-dev/kebi/src/kebi/core/agent/invocation.py` with `build_turn_payload(message, user_id, taste_profile_summary, memory_summary, location)` per the plan's M3 target shape and FR-022. Docstring notes: single construction site; resets both transient fields (`last_recall_results=None`, `reasoning_steps=[]`) in lockstep; sets `steps_taken=0`, `error_count=0`; wraps `message` as a single-element `[HumanMessage(content=message)]` list so the `add_messages` reducer appends to history.
- [X] T039 [US4] Create `/Users/saher/dev/repos/kebi-dev/kebi/src/kebi/core/agent/graph.py` per research.md R6 and the plan's M3 section. Contents: (a) `agent_node(llm, tools)` closure that binds `llm.bind_tools(tools)`, renders the system prompt from `get_config().prompts["agent"].content` substituting `{taste_profile_summary}` and `{memory_summary}` from state, calls the bound llm with `[SystemMessage(prompt), *state["messages"]]`, appends the response to messages, and returns a Command (or dict) update with `steps_taken += 1`; (b) `should_continue(state)` routing to `"fallback"` on `error_count >= config.agent.max_errors` or `steps_taken >= config.agent.max_steps`, `"tools"` if last message has `tool_calls`, else `"end"`; (c) `fallback_node(state)` per R6: condition-specific summary, one user-visible `ReasoningStep(step="fallback", source="fallback", tool_name=None, visibility="user")` appended by read-then-concat, plus one `AIMessage("Something went wrong on my side — try again with a bit more detail?")`; (d) `build_graph(llm, tools, checkpointer)` wiring nodes + conditional edges per FR-025.
- [X] T040 [P] [US4] Create `/Users/saher/dev/repos/kebi-dev/kebi/src/kebi/core/agent/checkpointer.py` with `async def build_checkpointer() -> AsyncPostgresSaver` per research.md R2: construct via `AsyncPostgresSaver.from_conn_string(get_secrets().DATABASE_URL)`; `await saver.setup()` on first call (idempotent); return the instance. Docstring notes: lazy construction, cached by caller (M6), not called at startup (FR-018b).
- [X] T041 [P] [US4] Edit `/Users/saher/dev/repos/kebi-dev/kebi/alembic/env.py` per research.md R7 and FR-031: add module-level `_LIBRARY_TABLES = {"checkpoints", "checkpoint_blobs", "checkpoint_writes"}` and an `_include_object(object, name, type_, reflected, compare_to)` callable returning `False` when `type_ == "table" and name in _LIBRARY_TABLES`. Wire `include_object=_include_object` into BOTH `context.configure(...)` calls (online and offline).
- [X] T042 [US4] Run the US4 test slice (unit tests only first): `poetry run pytest tests/core/agent -v -k "not checkpointer and not graph_compile"` — green. Then the compile test: `poetry run pytest tests/core/agent/test_graph_compile.py -v`. Then the integration tests (requires docker-compose Postgres): `docker compose up -d postgres && poetry run pytest tests/core/agent/test_checkpointer.py tests/alembic/test_checkpointer_exclusion.py -v`. Finally `poetry run mypy src/` — clean.

**Checkpoint**: US4 complete. Agent module compiles, routes correctly, checkpointer is idempotent, Alembic ignores library tables. No `/v1/chat` wiring yet (that's M6).

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: End-to-end verification against the quickstart + full suite.

- [ ] T043 **MANUAL — operator to run**: Complete quickstart walkthrough at `/Users/saher/dev/repos/kebi-dev/kebi/specs/027-agent-foundation/quickstart.md` steps 1–13 against a clean local environment. Programmatic steps 3–8, 12, 13 already verified (574 tests green, ruff/mypy at baseline, `poetry run python -c "from kebi.core.config import get_config; print(get_config().agent.enabled, get_config().prompts['agent'].file)"` prints `False agent.txt`). Manual steps 9/10/11 (uvicorn + curl + redis-cli) require an operator session.
- [X] T044 [P] Run full suite green: `poetry run ruff check src/ tests/ && poetry run ruff format --check src/ tests/ && poetry run mypy src/ && poetry run pytest -x`. Any failure halts completion. **Final state**: pytest 574 passed / 2 skipped; mypy 1 pre-existing baseline error only; ruff 4 pre-existing errors (feature removed 1 baseline test error by rewriting); format has pre-existing drift in unrelated files (`test_handlers.py`, `test_types.py`, `chat/router.py`, `extraction/types.py`, `providers/tracing.py`, etc.); feature-touched files formatted clean.
- [ ] T045 [P] **MANUAL — operator to run**: Flip-the-flag smoke test — edit `config/app.yaml` setting `agent.enabled: true`, restart uvicorn, `poetry run python -c "from kebi.core.config import get_config; print(get_config().agent.enabled)"` prints `True`. Revert and verify no user-visible behavior change. Recorded in research.md addendum that config + prompt validation is eager at boot per FR-018a; flag is read per-request at dispatch (FR-018b) — no per-process cache to invalidate mid-flight because `/v1/chat` is not wired to the agent in this feature (that's M6).
- [X] T046 Update `/Users/saher/dev/repos/kebi-dev/kebi/CLAUDE.md` Recent Changes section with one line: `027-agent-foundation: ExtractPlaceResponse two-level status (ADR-063) + raw_input rename; Redis prefix extraction:v2; ExtractionService inline await; agent: config block + config/prompts/agent.txt; core/agent/ skeleton (state, reasoning, invocation, graph, checkpointer) with Postgres AsyncPostgresSaver; Alembic excludes checkpointer tables. Flag agent.enabled default false; no user-visible behavior change aside from the cleaner extraction envelope.`
- [X] T047 Fill in the `research.md` post-implement addendum (exact pinned `langgraph-checkpoint-postgres` version, Python 3.11 compatibility confirmation, first `extraction:v2:` write timestamp, first successful `AsyncPostgresSaver.setup()` run).
- [X] T048 Confirm the product-repo coordination constraint (FR-036) is acknowledged: the AI-repo PR description references the product-repo's matching PR URL (or notes "product-repo PR TBD before deploy"). No AI-repo deploy without the matching product-repo merge.

---

## Dependencies

### User Story Dependencies

- **US1 (P1 — M0.5)**: Can start after Phase 1+2 foundation. Blocks US2 (M1 assumes the new envelope shape) and defines the external contract that FR-036 pins to the product repo.
- **US2 (P1 — M1)**: Requires US1 complete. Internal refactor; no external surface change.
- **US3 (P2 — M2)**: Independent of US1/US2. Can run on a parallel stream.
- **US4 (P2 — M3)**: Requires US3 complete (reads `get_config().agent.max_steps/max_errors` in `should_continue`). No dependency on US1/US2.

### Within Each User Story

- Tests written and FAILing before implementation (TDD posture — plan decision since SC-001…SC-013 are test-verified).
- Models / schemas before services.
- Services before dispatch / route edits.
- Core implementation before docs + Bruno + ADR fill-in (for US1).
- US4 especially: `reasoning.py` before `state.py` (state imports ReasoningStep); `state.py` before `invocation.py` and `graph.py` (both read the state type); `checkpointer.py` is independent of state/graph.

### Parallel Opportunities

- **Setup phase**: T002 runs alongside T001 after the install completes.
- **US1**: T004/T005 parallel (different test files). T009/T010/T011/T012 parallel (different files: Redis constant, ADR, api-contract.md, Bruno).
- **US2**: T014/T015 parallel (different test files). T016/T017 are sequential (T017's fixture references the new signature T016 introduces).
- **US3**: T019 runs before T020/T021/T022 (TDD). T020/T021/T022 can run in parallel (config.py, app.yaml, agent.txt — three different files).
- **US4**: T024–T030 test files all parallel to each other. T034/T035/T036/T037/T040/T041 can mostly run in parallel; T038 depends on T035+T037; T039 depends on T035+T037+T038 (needs state, reasoning, invocation).
- **Across streams**: US1+US2 (P1 stream) and US3+US4 (P2 stream) have zero file overlap and can be developed concurrently.

---

## Parallel Example: User Story 4 test batch

```bash
# Launch all US4 test file creations together (T025–T030 target different test files):
Task: "Create tests/core/agent/test_reasoning.py with ReasoningStep validation tests"
Task: "Create tests/core/agent/test_state.py with AgentState + add_messages reducer tests"
Task: "Create tests/core/agent/test_invocation.py with build_turn_payload reset tests"
Task: "Create tests/core/agent/test_graph_routing.py with should_continue branch tests"
Task: "Create tests/core/agent/test_fallback.py with fallback_node emission tests"
Task: "Create tests/core/agent/test_agent_node.py with mocked-LLM structural tests"
```

---

## Implementation Strategy

### MVP (User Story 1 only)

1. Complete Phase 1 (T001–T002) and Phase 2 (T003).
2. Complete Phase 3 (T004–T013) — US1 lands with tests, schema rewrite, Redis prefix bump, ADR-063, docs, Bruno.
3. **STOP, VALIDATE**: `poetry run pytest tests/api/schemas/test_extract_place.py tests/api/routes/test_extraction.py -v`.
4. Coordinate product-repo merge per FR-036. Deploy US1 alone if desired — the extraction response shape is the only externally-visible change, and the internal paths still work with the old `_run_background` pattern until US2 lands.

### Incremental delivery (this feature's target path)

1. Setup + Foundational (T001–T003).
2. US1 (T004–T013) → deploy after product-repo coordination.
3. US2 (T014–T018) → deploy; internal refactor, no product-repo surface touched.
4. US3 (T019–T023) → deploy; pure-additive config block.
5. US4 (T024–T042) → deploy; new module exists but unwired to `/v1/chat`.
6. Polish (T043–T048) → final verification.

### Parallel team strategy

- **Stream A (P1)**: T004 → T013 then T014 → T018 (US1 then US2). One developer.
- **Stream B (P2)**: T019 → T023 then T024 → T042 (US3 then US4). One developer.
- Streams have zero file overlap. Integration at Phase 7.

---

## Notes

- [P] = different files, no in-phase dependencies.
- Every task lists exact absolute file paths to avoid ambiguity during `/speckit.implement` execution.
- TDD: write tests first, watch them fail, then implement. Plan explicitly chose this posture because SC-001…SC-013 are test-verified acceptance criteria.
- Commit per `.claude/rules/git.md`: `feat(<scope>): <description> #<task-id>`. Scopes align with module under edit (`api`, `intent`, `extraction`, `chat`, `config`, `agent`, `db`).
- US1 is the only externally-visible change; the other three stories ship behind `agent.enabled=false` and have no user-visible impact.
- Do NOT flip `agent.enabled` to `true` in this feature (that's M10 — explicitly out of scope).
- Do NOT touch `core/intent/`, `core/chat/router.py`, or `core/chat/chat_assistant_service.py` — legacy deletion is M11.
