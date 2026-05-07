# Feature Specification: Agent Foundation (M0.5 + M1 + M2 + M3)

**Feature Branch**: `027-agent-foundation`
**Created**: 2026-04-21
**Status**: Draft
**Input**: User description: "Implement milestones m0.5, M1, M2, and M3 from docs/plans/2026-04-21-agent-tool-migration.md: ExtractPlaceResponse schema cleanup (M0.5), ExtractionService inline await refactor (M1), agent system prompt and config scaffolding (M2), and the agent graph skeleton with state, nodes, and Postgres checkpointer (M3). Everything ships behind the agent_enabled feature flag with no user-facing behavior change."

**Source plan**: `docs/plans/2026-04-21-agent-tool-migration.md` (binding). Binding ADRs: ADR-062 (agent architecture), ADR-063 (to be added in M0.5: two-level extraction status), ADR-044 (prompt-injection mitigations), ADR-048 (extraction polling route), ADR-051 (secrets via `.env`), ADR-052 (`/v1/chat` unified), ADR-054/055/056/058 (PlaceObject + schema state), ADR-057 (confidence bands), ADR-059 (`config/prompts/`), ADR-060 (recommendations), ADR-061 (signal tiers).

## Clarifications

### Session 2026-04-21

- Q: When `agent.enabled=false`, which agent components initialize at FastAPI startup? → A: Validate config + prompt eagerly at boot (loud failure on missing or malformed prompt — it's our code); keep checkpointer and LLM-bound graph lazy (built on first `/v1/chat` call with flag on, then cached); flag is checked per-request at dispatch, not at startup — this lets M10 flip the flag in a running prod env without a redeploy.
- Q: What exactly does `ExtractPlaceResponse.raw_input` contain? → A: Verbatim — exact bytes as received from `/v1/chat`, no trimming, no URL canonicalization, no case-folding. The field is a pure echo of what the user submitted.
- Q: In M3, does `fallback_node` emit `ReasoningStep`s to state, or is emission deferred? → A: Emit one user-visible `fallback` ReasoningStep now (`source="fallback"`, `tool_name=None`, `visibility="user"`); defer the debug diagnostic steps (`max_steps_detail`, `max_errors_detail`) to M9 where Langfuse spans land.
- Q: How does `GET /v1/extraction/{request_id}` handle a request_id whose data only exists under the old `v1:` Redis prefix after the M0.5 deploy? → A: Return 404 — same code path the polling route already takes for TTL-expired keys, which the product repo already handles. Solo-dev / Railway context has no rolling-deploy overlap, and the practical rule is "don't deploy mid-extraction." No coercion layer is introduced; the legacy prefix is simply unread.
- Q: In M3, is `agent_node` wired to a real LLM provider, or is it structurally complete but exercised only with a mocked LLM? → A: Structurally complete, mocked-LLM tests only — `agent_node` takes an injected LLM (signature, prompt rendering, `steps_taken` increment, message append all implemented), but M3's test suite drives it with a fake/mocked LLM. Real orchestrator wiring through the provider abstraction lands in M6 as part of the lazy graph construction. M3 does not require `ANTHROPIC_API_KEY` in any test environment.

## User Scenarios & Testing *(mandatory)*

This feature introduces one externally-visible contract change (the extraction response envelope in M0.5, coordinated with the product repo) and three layers of internal foundation (M1 inline refactor, M2 config scaffolding, M3 agent graph skeleton). The agent itself is not yet wired to `/v1/chat` — that is M6. Everything here ships behind `agent_enabled=false`, so end users see no behavioral change beyond the cleaned-up extraction response shape.

"Users" in this spec are:

- **API consumers** (the product repo calling `/v1/chat` and `/v1/extraction/{request_id}`) — observe the new extraction response shape.
- **Operators** (people running the service) — toggle the `agent_enabled` flag and read the agent prompt from config.
- **Engineers** building later milestones (M4–M11) — depend on the schema, config, and graph skeleton landed here.

### User Story 1 - Cleaner extraction response contract (Priority: P1)

When a user shares a URL or place description through `/v1/chat`, the API returns an extraction response where pipeline-level status (`pending`, `completed`, `failed`) lives on the response envelope and per-place status (`saved`, `duplicate`, `needs_review`) lives on each item. Every item in the `results` list carries a real place and confidence — never null placeholders.

**Why this priority**: This is the only externally-visible change in the foundation feature and requires lockstep coordination with the product repo. Landing it first unblocks the product repo's matching schema update while internal work (M1–M3) proceeds in parallel, and prevents M1 from having to rewrite the response-construction code twice.

**Independent Test**: Call `POST /v1/chat` with a TikTok URL; assert the response `data.status="pending"`, `data.results=[]`, `data.request_id=<id>`, and `data.raw_input` echoes the original user string. Then call `GET /v1/extraction/{request_id}` and, after extraction completes, assert `data.status="completed"`, `data.raw_input` is still the original input, and each entry in `data.results` has a real place, real confidence, and a status value drawn only from `{saved, needs_review, duplicate}`. A mixed-outcome run (e.g., one saved place + one duplicate) returns two real items in one envelope with no null placeholders.

**Acceptance Scenarios**:

1. **Given** a `/v1/chat` request carrying a supported URL, **When** the chat service dispatches extraction, **Then** the response envelope carries `status="pending"` with an empty `results` list, a `request_id`, and a `raw_input` equal to the original user-supplied string.
2. **Given** a completed extraction producing at least one above-threshold outcome, **When** a client polls `GET /v1/extraction/{request_id}`, **Then** the envelope carries `status="completed"`, `raw_input` equals the original input, and each `results[i]` includes a non-null `place`, a non-null `confidence`, and a `status` value from `{saved, needs_review, duplicate}`.
3. **Given** an extraction where every candidate falls below the confidence threshold, **When** the pipeline finishes, **Then** the envelope carries `status="failed"` with `results=[]`, `raw_input` preserved, and no null-placeholder items anywhere in the payload.
4. **Given** an extraction that returns a mixture of saved and duplicate outcomes, **When** the client reads the response, **Then** it sees one envelope with `status="completed"` and two real items whose per-place statuses reflect their individual outcomes.
5. **Given** any response payload produced by the extraction code path, **When** the envelope is inspected, **Then** the original user input is carried in `raw_input` (never on a different field name such as `source_url`).

---

### User Story 2 - Save tool can observe the real extraction outcome inline (Priority: P1)

The `ExtractionService.run()` call returns the real pipeline outcome synchronously (no `asyncio.create_task` hidden inside the service). This lets downstream code — most importantly the agent's Save tool in M5 — obtain a meaningful status (`saved`, `duplicate`, `needs_review`, or envelope-level `failed`) without polling, while the existing HTTP route preserves its fire-and-return `pending` behavior by scheduling the background task at the route layer instead of inside the service.

**Why this priority**: Without inline await, the Save tool cannot compose a coherent agent response — it would only ever see `pending`. This refactor is the load-bearing change that makes the agent's save path possible, even though it is not yet wired to a tool in this feature.

**Independent Test**: Unit-test `ExtractionService.run()` by calling it directly and awaiting the coroutine; assert the returned envelope carries `status ∈ {completed, failed}` (never `pending`), that below-threshold candidates never appear in `results`, and that the Redis status store contains a payload matching the returned envelope. Separately, test `ChatService._dispatch_extraction` and assert that the externally-returned `ChatResponse` still carries `data.status="pending"` + `data.request_id` (HTTP behavior preserved) and that the background task writes the real envelope to Redis.

**Acceptance Scenarios**:

1. **Given** a caller of `ExtractionService.run(raw_input, user_id)`, **When** the coroutine is awaited, **Then** the returned envelope's `status` is `completed` or `failed` (never `pending`) and all items in `results` carry real places, real confidences, and per-place statuses.
2. **Given** the `/v1/chat` extract-place dispatch path, **When** a user submits a URL, **Then** the HTTP response still returns `status="pending"` with a `request_id` immediately, and a background task finishes the extraction and writes the real envelope to Redis under that request_id.
3. **Given** `ExtractionService.run()` is called and the pipeline produces no matches, **When** inspection of the response occurs, **Then** `status="failed"` and `results=[]` — there is no placeholder item with a null place.

---

### User Story 3 - Agent configuration and prompt are externalized and togglable (Priority: P2)

A single config knob (`agent.enabled`) gates the entire agent path; its default is `false` for this feature. The agent's system prompt lives on disk at `config/prompts/agent.txt` (not embedded in Python), is registered via `config/app.yaml`, and takes two template slots (`{taste_profile_summary}`, `{memory_summary}`) that will be filled at invocation time. Per-tool timeouts (`recall`, `consult`, `save`), failure budget (`max_errors`), and step ceiling (`max_steps`) are declared in config, not hardcoded.

**Why this priority**: The prompt and timeouts are operator-facing and must be tunable without code changes. They also act as the typed contract that M3's graph skeleton and M5's tool wrappers will read. Landing this first means later milestones simply consume config rather than defining it.

**Independent Test**: Load `get_config()` and assert `config.agent.enabled is False`, that `config.agent.max_steps`, `max_errors`, `checkpointer_ttl_seconds`, and the three per-tool timeouts are typed integers with the documented defaults, and that `config.prompts["agent"]` resolves to `agent.txt`. Open `config/prompts/agent.txt` and assert it is a readable text file containing the two template slots `{taste_profile_summary}` and `{memory_summary}` and the persona + safety sections described in the plan.

**Acceptance Scenarios**:

1. **Given** a fresh checkout at this milestone, **When** `get_config()` is called, **Then** it returns a typed `AgentConfig` with `enabled=false`, the documented default timeouts (recall=5, consult=10, save=25), `max_steps=10`, `max_errors=3`, `checkpointer_ttl_seconds=86400`.
2. **Given** the registered agent prompt, **When** the file is loaded, **Then** it renders cleanly after string-substituting `{taste_profile_summary}` and `{memory_summary}` — both slots are present and named exactly as specified.
3. **Given** an operator toggling `agent.enabled: true` in `config/app.yaml`, **When** `get_config()` is re-evaluated, **Then** the flag flips without any code change required.

---

### User Story 4 - Agent graph skeleton compiles and routes correctly (Priority: P2)

A new `core/agent/` module provides: a typed `AgentState` with the documented fields (messages, taste/memory summaries, user_id, location, last_recall_results, reasoning_steps, steps_taken, error_count); a `build_turn_payload` helper that resets `last_recall_results` and `reasoning_steps` together on every new user message; a `ReasoningStep` Pydantic model with `source`, `tool_name`, `visibility`, `timestamp`; a compiled `StateGraph` with `agent`, `tools`, and `fallback` nodes and a `should_continue` router that respects the config-driven `max_steps` and `max_errors` ceilings; and a Postgres-backed checkpointer (`AsyncPostgresSaver`) whose `setup()` is idempotent and whose tables are excluded from Alembic's autogenerate.

**Why this priority**: The graph is the structural backbone M5–M9 bolt onto. Getting the state shape, the reset helper, the routing rules, and the checkpointer correct now prevents costly rework later. The acceptance criterion is structural — the graph compiles and routes correctly with mocked inputs. No LLM calls, no wiring to `/v1/chat`.

**Independent Test**: Build the graph with a fake LLM and fake tools using an `InMemorySaver` fixture. Drive `should_continue` through each branch (`tools`, `fallback`, `end`) with crafted states — verify the `error_count ≥ max_errors` and `steps_taken ≥ max_steps` rails both route to the fallback node. Call `build_turn_payload(...)` twice and confirm the two transient fields (`last_recall_results`, `reasoning_steps`) both reset to `None` / `[]` on each call while other fields are carried through. Spin up `AsyncPostgresSaver.from_conn_string(DATABASE_URL)` against the local Postgres, call `setup()` twice, confirm idempotency and that the three checkpointer tables exist. Run `alembic check` and confirm it does not flag the checkpointer tables.

**Acceptance Scenarios**:

1. **Given** the `core/agent/graph.py::build_graph` factory, **When** it is called with a mock LLM, mock tools, and an `InMemorySaver`, **Then** the graph compiles without error and exposes entry point `agent` with the expected node set `{agent, tools, fallback}`.
2. **Given** `should_continue` receives a state with `error_count >= max_errors`, **When** it is evaluated, **Then** it returns `"fallback"` regardless of message contents; likewise for `steps_taken >= max_steps`.
3. **Given** `should_continue` receives a state whose last `AIMessage` carries tool calls, **When** it is evaluated, **Then** it returns `"tools"`; if no tool calls, it returns `"end"`.
4. **Given** two consecutive calls to `build_turn_payload`, **When** the payloads are inspected, **Then** both `last_recall_results` and `reasoning_steps` are reset identically on each call (never partially).
5. **Given** a fresh Postgres database, **When** `build_checkpointer()` is awaited and `setup()` runs, **Then** the tables `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` exist; a second call to `setup()` is idempotent.
6. **Given** `alembic check` is run after the agent module is in place, **When** its output is inspected, **Then** the three checkpointer tables are excluded from autogenerate and no diffs are reported against them.
7. **Given** `should_continue` routes a state to `fallback` (via `max_steps` or `max_errors`), **When** `fallback_node` runs, **Then** `state["reasoning_steps"]` ends with exactly one entry whose `step="fallback"`, `source="fallback"`, `tool_name is None`, `visibility="user"`, and whose `summary` names the terminal condition — and no debug diagnostic steps are appended.

---

### Edge Cases

- **Extraction pipeline returns nothing**: envelope is `status="failed"`, `results=[]`. No null-placeholder item is synthesized.
- **Extraction returns only below-threshold candidates**: envelope is `status="failed"`, `results=[]` — below-threshold outcomes never appear in `results`.
- **Extraction returns a mix of above-threshold and below-threshold candidates**: envelope is `status="completed"`, `results` contains only the above-threshold items in their natural per-place status.
- **Redis key collision across the schema rollout**: the old `ExtractPlaceResponse` shape and the new one disagree on field nullability, so the Redis key prefix is bumped (e.g., `extraction:v2:{request_id}`) so pre-deploy and post-deploy writes cannot be misread by post-deploy readers.
- **Polling for a pre-deploy `request_id` after the prefix bump**: the polling route reads only `extraction:v2:*` keys and returns `404` for any `request_id` not found under that prefix — the same behavior it already produces for TTL-expired keys, which the product repo already handles. No legacy-prefix read path or coercion layer is introduced; operators are expected to avoid mid-extraction redeploys (solo-dev / Railway context has no rolling-deploy overlap).
- **Legacy HTTP path (flag off)**: `ChatService._dispatch_extraction` still returns `pending` immediately and runs extraction in a background task at the route layer — behavior unchanged from the product repo's perspective.
- **Agent prompt is missing or malformed at startup**: startup aborts with a clear error naming the problem (missing file, missing template slot, unreadable content) — regardless of the flag value, since the prompt file is part of the committed codebase and any issue with it is a code bug, not a runtime condition. Operators see a loud boot-time failure rather than a silent fallback or a latent crash on first `/v1/chat`.
- **`DATABASE_URL` unreachable at startup with flag off**: boot succeeds — the checkpointer is lazy and is never constructed unless `/v1/chat` is called with the flag on.
- **`agent.enabled` flipped from false to true in a running process**: the next `/v1/chat` request observes the new flag value and triggers one-time lazy construction of the checkpointer + graph; subsequent requests reuse the cached instances. No redeploy is required to flip the flag.
- **Postgres checkpointer tables already exist**: `setup()` is idempotent — running it twice does not fail and does not overwrite existing checkpoints.
- **Alembic autogenerate runs after the checkpointer is installed**: the three library-owned tables are excluded via the `include_object` filter, so Alembic never tries to migrate them.
- **Transient fields drift across turns**: impossible — `build_turn_payload` is the single construction site and resets both `last_recall_results` and `reasoning_steps` in one place; any future invocation site must route through it.
- **Two concurrent chat turns for the same `user_id`**: this feature does not add any new protection, but the checkpointer thread key is `user_id` and LangGraph state-merge semantics apply. Concurrency behavior under that thread key is inherited from `langgraph-checkpoint-postgres`; verifying it at load is deferred to M9.

## Requirements *(mandatory)*

### Functional Requirements

**M0.5 — ExtractPlaceResponse schema cleanup**

- **FR-001**: `ExtractPlaceResponse` MUST carry pipeline-level status (`pending` | `completed` | `failed`) on the envelope only.
- **FR-002**: `ExtractPlaceResponse.results` MUST be empty whenever `status != "completed"`.
- **FR-003**: `ExtractPlaceItem` MUST carry non-null `place`, non-null `confidence`, and a per-place `status` drawn exclusively from `{saved, needs_review, duplicate}`.
- **FR-004**: The extraction code path MUST NOT produce null-placeholder items for pipeline-level states; pipeline states live on the envelope only.
- **FR-005**: Below-threshold extraction outcomes MUST NOT appear in `results`; they influence only the envelope-level `failed` determination.
- **FR-006**: `ExtractPlaceResponse` MUST expose the original user-supplied input as `raw_input: str | None` (the free-text or URL the user submitted) — replacing the previous `source_url` field. `raw_input` MUST carry the user input **verbatim**: exact bytes as received from `/v1/chat`, no whitespace trimming, no URL canonicalization, no case-folding. It is a pure echo field. Any normalization the extraction pipeline needs internally MUST operate on its own copy and MUST NOT mutate this envelope field. This is a single, non-backwards-compatible rename; no alias or dual-write. The polling route `GET /v1/extraction/{request_id}` MUST return the new envelope shape, and the Redis key prefix used by `ExtractionService` MUST be bumped (`extraction:v2:{request_id}`) to isolate the rollout from any still-cached v1 payloads.
- **FR-007**: `docs/api-contract.md` MUST be updated to reflect the new envelope shape (two-level status + `raw_input` field), and an ADR-063 MUST be added to `docs/decisions.md` recording the two-level-status decision, the `source_url → raw_input` rename, and the product-repo coordination.
- **FR-008**: The Bruno collection at `kebi-config/bruno/` MUST be updated so example requests/responses match the new shape.
- **FR-008a** (polling-route legacy behavior — per clarification 2026-04-21): `GET /v1/extraction/{request_id}` MUST read only the bumped `extraction:v2:{request_id}` prefix. For any `request_id` not present under that prefix, the route MUST return `404` — identical to the existing TTL-expired path. The route MUST NOT fall back to reading `extraction:v1:*`, MUST NOT coerce or upgrade legacy payloads in-flight, and MUST NOT introduce any compatibility shim tied to the deploy window.

**M1 — ExtractionService inline await**

- **FR-009**: `ExtractionService.run()` MUST await the extraction pipeline inline and return the real envelope (status in `{completed, failed}`) synchronously; the previous `asyncio.create_task` inside `run()` MUST be removed.
- **FR-010**: `ExtractionService.run()` MUST write the final envelope to the Redis status store as part of `run()`'s critical path (not from a detached task).
- **FR-011**: `ChatService._dispatch_extraction` MUST preserve the existing HTTP behavior by scheduling the inline-await call in an `asyncio.create_task` at the route layer, returning `status="pending"` + `request_id` immediately.
- **FR-012**: `ChatService._dispatch_extraction` MUST read `extract_result.status` from the envelope (not infer it by scanning `results` for a `pending` item).
- **FR-013**: The `_outcome_to_dict` helper MUST be replaced by an `_outcome_to_item_dict` that maps only real outcomes (`saved`, `needs_review`, `duplicate`) and an `_is_real` predicate that filters below-threshold outcomes out of `results`.

**M2 — Agent system prompt and config scaffolding**

- **FR-014**: `config/app.yaml` MUST gain an `agent:` block with `enabled: false`, `max_steps: 10`, `max_errors: 3`, `checkpointer_ttl_seconds: 86400`, and `tool_timeouts_seconds` with `recall: 5`, `consult: 10`, `save: 25`.
- **FR-015**: `config/app.yaml`'s `prompts:` block MUST register `agent: agent.txt` alongside the existing entries.
- **FR-016**: A new file `config/prompts/agent.txt` MUST exist containing the places-advisor persona (covering restaurants, bars, cafes, museums, shops, hotels, services — not a food-only persona), three-tool usage guidance at a high level (recall / save / consult — no per-arg shaping rules), the ADR-044 prompt-injection mitigations, and exactly two template slots: `{taste_profile_summary}` and `{memory_summary}`.
- **FR-017**: `src/kebi/core/config.py` MUST expose a typed nested `AgentConfig` (with nested `ToolTimeoutsConfig`) accessible as `get_config().agent`.
- **FR-018**: No caller in this feature's scope reads `config.agent.*` yet — wiring happens in M3 (graph) and M5 (tools). The typed shape MUST compile under `mypy --strict`.
- **FR-018a** (boot-time wiring — per clarification 2026-04-21): At FastAPI startup, `config/app.yaml` and `config/prompts/agent.txt` MUST be loaded and validated eagerly. Prompt validation MUST verify that both template slots `{taste_profile_summary}` and `{memory_summary}` are present; any missing slot, missing file, or malformed config MUST abort startup with a clear error message. Startup MUST NOT depend on `DATABASE_URL` being reachable or on `ANTHROPIC_API_KEY` being present — neither is read at boot.
- **FR-018b** (lazy agent infrastructure — per clarification 2026-04-21): `build_checkpointer()` and the LLM-bound compiled graph MUST be constructed lazily, only on the first `/v1/chat` request observed with `agent.enabled=true`, and cached thereafter. The flag MUST be evaluated per-request at the dispatch site (not captured at process start), so `agent.enabled` can be flipped in a running production environment without a redeploy. Neither the graph nor the checkpointer is initialized at startup regardless of flag value.

**M3 — Agent graph skeleton**

- **FR-019**: A new module `src/kebi/core/agent/` MUST exist containing at minimum: `state.py` (`AgentState` TypedDict), `invocation.py` (`build_turn_payload` helper), `reasoning.py` (`ReasoningStep` Pydantic model), `graph.py` (`build_graph` factory + `should_continue` router + `fallback_node`), `checkpointer.py` (`build_checkpointer` coroutine).
- **FR-020**: `AgentState` MUST carry the fields: `messages: Annotated[list[BaseMessage], add_messages]`, `taste_profile_summary: str`, `memory_summary: str`, `user_id: str`, `location: dict | None`, `last_recall_results: list[PlaceObject] | None`, `reasoning_steps: list[ReasoningStep]`, `steps_taken: int`, `error_count: int`.
- **FR-021**: `reasoning_steps` MUST NOT use a reducer — plain overwrite semantics, with tools appending by reading `runtime.state.get("reasoning_steps")` and returning the concatenated list in their `Command(update=...)`.
- **FR-022**: `build_turn_payload(message, user_id, taste_profile_summary, memory_summary, location)` MUST be the single construction site for per-turn state updates. It MUST reset `last_recall_results` to `None` and `reasoning_steps` to `[]` on every call, and set `steps_taken=0`, `error_count=0`.
- **FR-023**: `ReasoningStep` MUST be a Pydantic model carrying `step: str`, `summary: str`, `source: Literal["tool", "agent", "fallback"]`, `tool_name: Literal["recall", "save", "consult"] | None`, `visibility: Literal["user", "debug"]`, `timestamp: datetime` (defaulted to now UTC).
- **FR-024**: The existing `api/schemas/consult.py::ReasoningStep` MUST be replaced by a re-export of the new `ReasoningStep` so `ConsultResponse.reasoning_steps` continues to type-check under the richer schema.
- **FR-025**: `build_graph(llm, tools, checkpointer)` MUST construct a `StateGraph(AgentState)` with nodes `agent`, `tools` (`ToolNode(tools)`), `fallback` (the composed graceful message), entry point `agent`, conditional edges from `agent` via `should_continue` to `{tools, fallback, end}`, a direct edge from `tools` back to `agent`, and an edge from `fallback` to END. It MUST compile with the provided checkpointer.
- **FR-026**: `should_continue(state)` MUST route to `fallback` when `state["error_count"] >= config.agent.max_errors`, to `fallback` when `state["steps_taken"] >= config.agent.max_steps`, to `tools` when the last message has tool calls, and to `end` otherwise.
- **FR-027**: `fallback_node` MUST compose a user-facing `AIMessage("Something went wrong on my side — try again with a bit more detail?")` and set it on `state["messages"]`. It MUST also append exactly one user-visible `ReasoningStep` to `state["reasoning_steps"]` with `step="fallback"`, `source="fallback"`, `tool_name=None`, `visibility="user"`, and a short `summary` explaining the terminal condition (e.g., `"Got stuck after N steps"` or `"Hit too many errors"`). Debug diagnostic steps (`max_steps_detail`, `max_errors_detail`) are explicitly deferred to M9 and MUST NOT be emitted in this feature.
- **FR-028**: `agent_node` MUST call `llm.bind_tools(tools)`, render the system prompt with `taste_profile_summary` and `memory_summary` substituted from state, increment `steps_taken`, and append the LLM response to `messages`. The node MUST accept an injected LLM (not construct one itself); M3 tests MUST drive it with a fake/mocked LLM only. Real provider-abstraction wiring to `claude-sonnet-4-6` lands in M6's lazy graph construction. This feature MUST NOT require `ANTHROPIC_API_KEY` in any test or boot environment.
- **FR-029**: `pyproject.toml` MUST pin `langgraph-checkpoint-postgres` at a verified minor version (`^2.0` pending verification against the current PyPI latest at install time).
- **FR-030**: `build_checkpointer()` MUST return an `AsyncPostgresSaver.from_conn_string(get_secrets().DATABASE_URL)` and MUST await `setup()` on first call; `setup()` MUST be idempotent (repeated calls do not fail and do not overwrite existing state).
- **FR-031**: `alembic/env.py` MUST include an `include_object` filter that excludes the library-managed tables `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` from autogenerate, and the Alembic migration directory MUST NOT contain migrations for these tables.
- **FR-032**: Unit tests for the graph skeleton MUST use `InMemorySaver` (not Postgres); Postgres round-trips are deferred to M6 integration tests.

**Cross-cutting (applies to all four milestones)**

- **FR-033**: All code changes MUST pass `poetry run ruff check src/ tests/`, `poetry run ruff format --check src/ tests/`, `poetry run mypy src/`, and `poetry run pytest -x`.
- **FR-034**: The `agent_enabled` flag MUST default to `false` at the end of this feature. No code path reads from the agent graph yet from `/v1/chat`.
- **FR-035**: No user-visible behavior MUST change when `agent_enabled=false`, aside from the cleaner extraction envelope shape in FR-001 through FR-008 (which is intentional and coordinated with the product repo).
- **FR-036**: The product-repo schema update for ExtractPlaceResponse MUST be merged before M0.5's final deploy. This is a binding coordination constraint, not a soft dependency.

### Key Entities

- **ExtractPlaceResponse**: Pipeline-level envelope returned by `ExtractionService.run()` and the polling route. Carries `status ∈ {pending, completed, failed}`, `results: list[ExtractPlaceItem]`, `raw_input: str | None` (the original text / URL the user shared — **verbatim**, no normalization; replaces the previous `source_url` field for generality), `request_id: str | None`. `results` is empty whenever `status != "completed"`.
- **ExtractPlaceItem**: Per-place outcome. Carries a non-null `place: PlaceObject`, non-null `confidence: float`, and a `status ∈ {saved, needs_review, duplicate}`. No null placeholders.
- **AgentConfig**: Typed configuration block for the agent, nested under `AppConfig`. Fields: `enabled: bool`, `max_steps: int`, `max_errors: int`, `checkpointer_ttl_seconds: int`, `tool_timeouts_seconds: ToolTimeoutsConfig`. All read from `config/app.yaml`.
- **ToolTimeoutsConfig**: Per-tool timeout budget in seconds. Fields: `recall: int`, `consult: int`, `save: int`. Defaults `5 / 10 / 25`.
- **AgentState**: LangGraph TypedDict describing one turn's worth of state. Carries conversation messages (reducer: `add_messages`), session-scoped summaries (taste + memory), immutable turn inputs (user_id, location), and transient per-turn fields (`last_recall_results`, `reasoning_steps`, `steps_taken`, `error_count`).
- **ReasoningStep**: Pydantic model describing one entry in the agent's reasoning trace. Carries `step`, `summary`, `source ∈ {tool, agent, fallback}`, optional `tool_name ∈ {recall, save, consult}`, `visibility ∈ {user, debug}`, `timestamp`. Consumers filter by `visibility` to decide what lands in the JSON payload vs Langfuse.
- **AgentPrompt**: Text file at `config/prompts/agent.txt` carrying the places-advisor persona, high-level tool-usage guidance, and ADR-044 safety rules. Two template slots: `{taste_profile_summary}`, `{memory_summary}`. No per-tool arg-shaping rules (those live on each tool's `@tool` docstring, introduced in M5).
- **Checkpointer**: `AsyncPostgresSaver` (from `langgraph-checkpoint-postgres`) backed by the existing Railway Postgres at `DATABASE_URL`. Thread key is `user_id`. Library owns its three tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`) — Alembic excludes them.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A client polling `GET /v1/extraction/{request_id}` after a multi-place extraction receives exactly one envelope where `status="completed"` and every `results[i]` carries a non-null place, non-null confidence, and a per-place status from `{saved, needs_review, duplicate}` — 100% of integration-tested multi-place extractions satisfy this.
- **SC-002**: Zero items in any extraction response payload (envelope or polling) contain `place=None` or `confidence=None` at the item level. Verified by schema tests asserting `ExtractPlaceItem.place` and `ExtractPlaceItem.confidence` are non-Optional.
- **SC-003**: `ExtractionService.run()` awaited in a unit test returns an envelope whose status is `completed` or `failed` (never `pending`) within the extraction pipeline's own latency budget — no hidden background scheduling in the service.
- **SC-004**: The `/v1/chat` extract-place dispatch path returns `status="pending"` + `request_id` immediately on 100% of test invocations, with the real envelope landing in Redis under the bumped (`extraction:v2:`) prefix within the pipeline's typical budget. Product repo sees no latency regression on this path.
- **SC-005**: `get_config().agent.enabled` equals `False` on a fresh checkout. Flipping the YAML to `true` makes `get_config().agent.enabled` return `True` without any code change.
- **SC-006**: `config/prompts/agent.txt` loads, string-substitutes the two documented slots (`{taste_profile_summary}`, `{memory_summary}`), and contains the persona, tool-guidance, and ADR-044 safety-rule sections — verified by a content test.
- **SC-007**: `build_graph(llm, tools, InMemorySaver())` compiles without error and exposes the expected node set `{agent, tools, fallback}` with entry point `agent`. Verified by a structural test that does not call any LLM.
- **SC-008**: `should_continue` routes correctly on 100% of unit-tested state inputs covering all four branches (`tools`, `end`, `fallback via max_steps`, `fallback via max_errors`).
- **SC-009**: `build_turn_payload(...)` resets `last_recall_results=None` and `reasoning_steps=[]` on every call — zero test invocations find either field carried over from a prior call.
- **SC-010**: `AsyncPostgresSaver.setup()` is idempotent — two sequential calls against the local Postgres complete without raising and leave the three library-owned tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`) present. Verified by an integration-style test against the docker-compose Postgres.
- **SC-011**: `poetry run alembic check` reports no diffs against the three library-owned checkpointer tables after the `include_object` filter is installed.
- **SC-012**: `poetry run ruff check`, `poetry run ruff format --check`, `poetry run mypy src/`, and `poetry run pytest` all pass at the end of the feature. Zero new warnings introduced.
- **SC-013**: With `agent.enabled=false`, the existing `/v1/chat` test suite passes unchanged. The only externally-observable difference between pre-feature and post-feature behavior is the ExtractPlaceResponse envelope shape from SC-001/SC-002 — no other surface changes.

## Assumptions

- **A1**: The product repo will ship its matching `ExtractPlaceResponse` schema update (TypeScript types + any consumers reading `results[0].status`) in lockstep with M0.5's deploy. Without that sync, the deploy is blocked.
- **A2**: The Redis prefix bump approach (`extraction:v2:`) is the accepted rollout strategy (one-hour TTL means v1-prefixed payloads disappear within a deploy window; the bump isolates reads during that window).
- **A3**: The existing Railway Postgres instance at `DATABASE_URL` is acceptable as the checkpointer's backing store. `langgraph-checkpoint-postgres` ~2.0 is the current maintained version (verify latest at install).
- **A4**: Postgres checkpointing adds ~10–50ms per write; this is acceptable for conversational turns whose LLM latency dominates. Pinned as a deferred-observation risk in the plan, not a blocker.
- **A5**: Postgres has no Redis-style TTL; checkpoint cleanup is documented and deferred. The `checkpointer_ttl_seconds` config field lands now for future use.
- **A6**: `docker-compose` in the repo provides a local Postgres that `build_checkpointer()` can target for integration testing. The existing docker-compose service is not re-provisioned in this feature.
- **A7**: LangGraph's default state-merge semantics overwrite non-reducer fields; this is intentional for `last_recall_results` and `reasoning_steps` so a plain-overwrite `{"reasoning_steps": []}` in the invocation payload resets them.
- **A8**: `orchestrator` role already points to `claude-sonnet-4-6` in `config/app.yaml` (per the plan's "already done" section); this feature does not retouch that role.
- **A9**: Sequential tool execution within a turn — `ToolNode` does not parallelize Sonnet's single tool call per response, so there is no multi-writer race on `reasoning_steps`.
- **A10**: Tests in this feature do not hit the real LLM. Mocked LLM and mocked services drive graph/state unit tests; the `InMemorySaver` fixture from the checkpointer package substitutes for Postgres.

## Out of Scope

The following are explicitly deferred to later milestones (per the plan) and MUST NOT be attempted in this feature:

- **M4** — dropping `IntentParser` from `ConsultService` and the nested `PlaceFilters` hierarchy.
- **M5** — tool wrappers (recall/save/consult) and their `@tool`-docstring contracts.
- **M6** — wiring `/v1/chat` to the agent graph behind the flag.
- **M7** — SSE reasoning-step streaming endpoint.
- **M8** — `NodeInterrupt` for `needs_review` saves.
- **M9** — per-tool `asyncio.wait_for` guards, operational failure-budget hardening, Langfuse spans around each tool.
- **M10** — flipping `agent.enabled` to `true` by default.
- **M11** — deletion of the legacy intent pipeline, docs updates, ADR-064, and the `orchestrator → agent` rename.
- Parallelizing recall + consult discovery (ADR-050).
- Per-user feature flags or A/B rollout (single global flag is the contract).
- Checkpoint cleanup job (Postgres has no native TTL; deferred).
- Migrating any other hardcoded prompts into `config/prompts/` beyond the agent prompt.
