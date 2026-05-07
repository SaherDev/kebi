# Implementation Plan: Agent Tools & Chat Wiring (M4 + M5 + M6)

**Branch**: `028-agent-tools-wiring` | **Date**: 2026-04-22 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/Users/saher/dev/repos/kebi-dev/kebi/specs/028-agent-tools-wiring/spec.md`
**Source of truth**: `docs/plans/2026-04-21-agent-tool-migration.md` (milestones M4 / M5 / M6). Binding ADRs: ADR-019, ADR-025, ADR-044, ADR-052, ADR-056, ADR-057, ADR-058, ADR-060, ADR-061, ADR-062, plus the foundation artifacts landed in feature 027 (ADR-063, `core/agent/` skeleton, `PlaceFilters`/`RecallFilters` nested shape, `ReasoningStep` richer schema).

## Summary

Make the LangGraph agent skeleton from feature 027 actually runnable by (a) giving it a tool-friendly `ConsultService` to wrap, (b) writing the three tool wrappers, and (c) making `POST /v1/chat` honor the `agent.enabled` flag. Three concrete deliverables:

1. **M4** — Drop `IntentParser` from `ConsultService`. New signature takes pre-parsed `query`, pre-built `ConsultFilters`, pre-loaded `saved_places: list[PlaceObject]`, optional `preference_context: str | None`, `Location | None`, `signal_tier`, and an optional `emit: EmitFn | None = None` callback. Remove the internal `RecallService.run`, memory-load, and taste-profile-load calls from the main consult path (the cheap chip-filtering taste read stays). **Introduce the cross-cutting `EmitFn` primitive callback pattern in a new `core/emit.py` module.** Services (recall, consult, extraction) each gain an optional `emit` parameter and call `emit(step_name, summary)` at each pipeline boundary with primitive string tuples. Services never construct `ReasoningStep` objects — the wrapper's closure adds `source` / `tool_name` / `visibility` / `timestamp` and fans out to the LangGraph stream writer. **`ConsultResponse.reasoning_steps` is deleted** from the response schema; `_persist_recommendation` no longer stores reasoning steps. Introduce a shared `PlaceFilters` base in `core/places/filters.py` that mirrors `PlaceObject` (ADR-056); refactor the existing nested `RecallFilters` in `core/recall/types.py` to extend it; add a new sibling `ConsultFilters`. Rewrite `recall_repository._build_where_clause` WHERE-clause assembly to use `PlaceFilters` (the nested attributes walk is already in place from feature 027's pulled-forward M4 refactor; this milestone formalizes the base). Temporarily keep the flag-off `ChatService._dispatch_consult` working by calling `RecallService.run` inline and passing the results as `saved_places` — **no fallback is left inside `ConsultService`**; it fails loudly if `saved_places` is unset (per spec clarification Q2).

2. **M5** — Build three `@tool`-decorated async wrappers under `core/agent/tools/`: `recall_tool`, `save_tool`, `consult_tool`. Each is produced by a `build_*_tool(service)` factory (closure-based DI — plan option A) and returned in order by `build_tools(recall, extraction, consult)`. `user_id` and `location` come from runtime state; `saved_places` flows tool→tool via `state["last_recall_results"]` and is NOT in the consult tool's LLM-visible `args_schema`. **All three wrappers share one emit-closure pattern via `core/agent/tools/_emit.py`** — `build_emit_closure(tool_name) -> (collected, emit)` and `append_summary(collected, tool_name, summary)`. The returned `emit` closure accepts an optional `duration_ms` argument (for services that measured work directly) and otherwise computes the delta from the previous emit's timestamp; `append_summary`'s `tool.summary` step carries the total tool-invocation elapsed time. Each wrapper builds the emit closure, passes `emit=emit` to the service, appends its user-visible `tool.summary`, and returns `Command(update={"reasoning_steps": prior + collected, ...})`. `ReasoningStep` (from 027) gets a `duration_ms: float | None = None` field added. The agent node (already built in 027) is extended to emit one `agent.tool_decision` user-visible step per LLM call, reading `AIMessage.content` truncated to 200 chars with a synthesized fallback when content is empty. **The agent prompt (`config/prompts/agent.txt`, shipped in 027) is updated** to instruct the orchestrator to emit one tool call per response and chain sequentially across turns — primary mitigation for the last-write-wins race on `AgentState.reasoning_steps` (the field has no reducer by design). No `asyncio.wait_for` guards ship in this feature — per spec clarification Q3, per-tool timeout enforcement is deferred to M9; the `agent.tool_timeouts_seconds` config values committed in 027 remain unused here.

3. **M6** — Wire the agent graph into `POST /v1/chat` behind `config.agent.enabled`. `ChatService.run` forks: flag-off → existing `_run_legacy` (classify_intent + dispatch, unchanged); flag-on → new `_run_agent` path. `_run_agent` loads `taste_profile_summary` + `memory_summary` once, calls `build_turn_payload` to reset transient fields, invokes `graph.ainvoke(payload, config={"configurable": {"thread_id": user_id}})`, filters `reasoning_steps` to `visibility="user"` for the JSON payload, and emits `ChatResponse(type="agent", ...)` — the new literal per spec clarification Q1. Graph is constructed once at startup via a new `get_agent_graph` dependency (cached in app state) that `await build_checkpointer()` + `build_tools(...)` + `llm = get_llm("orchestrator")` + `build_graph(llm, tools, checkpointer)`. Upgrade existing `api/schemas/chat.py::ChatResponse.type: str` to a `Literal[...]` including the new `"agent"` value (additive — the spec clarification Q1 says additive to the existing Literal; today the field is typed `str` so we tighten to Literal in this feature).

Per spec clarifications: response type = new literal `"agent"` (Q1); flag-off consult dispatch calls `RecallService.run` inline and passes `saved_places` (Q2); no per-tool wall-clock timeouts in this feature (Q3, deferred to M9).

NodeInterrupt for `needs_review` (M8), SSE streaming endpoint (M7), tool-timeout enforcement (M9), flag flip (M10), and legacy deletion (M11) are all out of scope for this feature.

## Technical Context

**Language/Version**: Python 3.11 (`>=3.11,<3.14` per `pyproject.toml`; constitution ADR-006 bounds 3.11–3.12)
**Primary Dependencies**: FastAPI ^0.115, Pydantic ^2.10, pydantic-settings ^2.7, LangChain ^0.3, LangGraph ^0.3, langchain-anthropic ^0.3, langgraph-checkpoint-postgres ^3.0.5, psycopg[binary] ^3.3.3, SQLAlchemy ^2.0 async, asyncpg ^0.30, Alembic ^1.14, redis ^5.0, Instructor ^1.0, Langfuse ^3.0
**No new top-level dependency** — everything needed (LangGraph `StateGraph`, `ToolNode`, `@tool`, `Command`, `ToolRuntime`, `InMemorySaver`, `AsyncPostgresSaver`) is already on `pyproject.toml` from feature 027.
**Storage**: PostgreSQL via asyncpg (Railway; local via docker-compose). Library-managed checkpointer tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`) already exist from 027 and remain excluded from Alembic autogenerate. Redis remains the extraction status backend (`extraction:v2:*`).
**Testing**: pytest ^8.3 with `asyncio_mode = "auto"`. `InMemorySaver` from `langgraph-checkpoint` for graph tests (no Postgres round-trip). `FakeChatModel` / hand-stubbed LLM-like object with `bind_tools`, `ainvoke`, `tool_calls` attribute for agent-node and recall-consult-chain tests. Mocked `ToolRuntime` for isolated tool-wrapper tests.
**Target Platform**: Linux server (Railway). Local dev via docker-compose.
**Project Type**: Web service (FastAPI). Single-project repo per ADR-001 (src layout).
**Performance Goals**: SC-012 — P95 under 4s for recall-only agent turns, under 8s for consult-with-discovery turns, measured over a 20-request smoke run with `agent.enabled=true` in dev. Flag-off path preserves the 027 baseline exactly (SC-001).
**Constraints**: Flag stays `enabled: false` in the shipped `config/app.yaml` (FR-002). Zero regression on existing legacy-path tests (FR-003, SC-001). Graph construction is eager at startup regardless of flag value (to make flag-flip zero-latency); if `DATABASE_URL` is unreachable at startup with flag-off, boot still fails because the checkpointer builds eagerly — this is the same startup semantics as feature 027 and is acceptable for this feature. (A gated-lazy-build alternative is rejected below.)
**Scale/Scope**: Three milestone slices. Estimated surface: (M4) 1 new `core/places/filters.py`, 1 edit to `core/recall/types.py`, 1 edit to `db/repositories/recall_repository.py` (formalize already-done refactor), 1 major edit to `core/consult/service.py` (signature + main-path simplification), 1 small edit to `core/chat/service.py::_dispatch` consult branch, 1 edit to `api/deps.py` (drop `IntentParser`/`UserMemoryService` from `get_consult_service` wiring); (M5) 4 new files under `core/agent/tools/` (`__init__.py`, `recall_tool.py`, `save_tool.py`, `consult_tool.py`) plus small helpers; (M6) 1 major edit to `core/chat/service.py` (`_run_agent` path + flag fork), 1 edit to `api/schemas/chat.py` (Literal type + `ReasoningStep` re-export on `data`), 1 edit to `api/deps.py` (new `get_agent_graph` dependency + `ChatService` wiring with `agent_graph` param), 1 edit to `api/main.py` startup hook if needed to trigger graph construction. Test surface: ~10–14 new test files under `tests/core/agent/tools/` and `tests/core/agent/`. Estimated new LOC: 1200–1800 including tests.

All Technical Context entries resolved — no NEEDS CLARIFICATION markers.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Evaluated against `.specify/memory/constitution.md` (v1.0, 2026-03-08) plus binding ADRs beyond the constitution's static list (ADRs 048, 051, 052, 054–063). The constitution file codifies ADRs up to ADR-044; newer ADRs are binding per CLAUDE.md and the spec header note.

| Principle | Verdict | Notes |
|---|---|---|
| I. Repo Boundary (NON-NEGOTIABLE) | **PASS** | All changes are AI-side: consult service, agent tools, chat dispatch, `api/deps.py`, `api/schemas/chat.py`. No UI / auth / product CRUD touched. No imports from the product repo. FR-036 limits product-repo coordination to the `"agent"` response type value which stays dark until M10. |
| II. Architecture Decisions are Constraints | **PASS** | Implements M4/M5/M6 of ADR-062's rollout. No existing ADR contradicted. No new ADR required — the `PlaceFilters` base type is an extension of ADR-056 (place-object shape), the tool contracts are an extension of ADR-062. The new `ChatResponse.type="agent"` value is additive; no supersession of ADR-052 (which only mandates `/v1/chat` as the unified entry — this feature preserves that). |
| III. Provider Abstraction (NON-NEGOTIABLE) | **PASS** | `get_agent_graph` calls `get_llm("orchestrator")` (logical role) and passes the returned LLM into `build_graph`. No hardcoded model names anywhere. Tool wrappers wrap existing services (`RecallService`, `ExtractionService`, `ConsultService`) — all of which already route through the provider-abstraction layer. |
| IV. Pydantic Everywhere | **PASS** | `PlaceFilters`, `ConsultFilters`, and all three tool input schemas (`RecallToolInput`, `SaveToolInput`, `ConsultToolInput`) are Pydantic models. `AgentState` remains a `TypedDict` (LangGraph requirement, already justified in feature 027's constitution check — internal to the graph, not a function-boundary type). `ChatResponse.data` continues to be `dict[str, Any]`; we typ-narrow its reasoning-step contents via `ReasoningStep.model_dump()` at the mapping site, which is the same pattern used by `ConsultResponse.reasoning_steps` today. |
| V. Configuration Rules | **PASS** | `agent.enabled` / `agent.max_steps` / `agent.max_errors` / `agent.tool_timeouts_seconds.*` already live in `config/app.yaml` from 027 — this feature reads them but adds no new config. No secrets added. `get_config()` / `get_secrets()` remain the only access points. |
| VI. Database Write Ownership | **PASS** | No new tables. Checkpointer remains library-owned (from 027). Recommendation persistence stays inside `ConsultService._persist_recommendation`. No product tables touched. No embedding-dimension drift. |
| VII. Redis Ownership | **PASS** | No Redis schema changes. Extraction still uses `extraction:v2:*` (from 027). Tool wrappers do not touch Redis directly — they route through `ExtractionService` / `RecallService` / `ConsultService`, all of which own their own Redis access. |
| VIII. API Contract | **PASS** | `POST /v1/chat` preserved at the route level; the shape changes only by admitting a new `type="agent"` value on the agent path. Flag-off default keeps every existing consumer unaffected until M10. Bruno update is minimal (one new example response for the agent path) and gated on eventual cutover — docs-only in this feature. |
| IX. Testing | **PASS** | pytest coverage matches `src/` layout. `mypy --strict` must pass. `ruff check` must pass. Every new module gets a test file (FR-035 enumerates the coverage targets: tool schemas, state-reading, recall→consult handoff, per-turn reset, visibility filter, four reasoning-step invariants, decision truncation, decision fallback-on-empty). |
| X. Git & Commits | **PASS** | Commits on branch `028-agent-tools-wiring` follow `type(scope): description` with `#TASK_ID` trailer. Feature branch from `dev`, merge to `dev`. No new route → no new `.bru` file required by constitution, but we add one `.bru` example for the agent-path response to serve as a live contract reference (same pattern as 027 did for the ExtractPlaceResponse shape). |
| ADR-019 (`Depends()` only) | **PASS** | All new services wired via `api/deps.py` (new `get_agent_graph`); no construction inside route handlers. |
| ADR-025 (Langfuse on every LLM call) | **PASS** | Orchestrator LLM inherits the existing Langfuse handler attached by the provider factory; `graph.ainvoke` propagates callbacks via LangGraph's default config plumbing. FR-032 mandates tracing on every LLM and every tool invocation; this is verified in the M10 canary but the infrastructure ships here. |
| ADR-044 (prompt-injection mitigations) | **PASS** | Agent system prompt (from 027) already carries the three mitigations. Tool docstrings are data, not executable instructions — injected content returned by tools flows as `ToolMessage` content which Sonnet treats as untrusted per the system-prompt instruction. No new mitigation surface required. |
| ADR-052 (`/v1/chat` unified) | **PASS** | No new routes. Agent path coexists with legacy under a single flag. |
| ADR-056 (PlaceObject unified shape) | **PASS** | New `PlaceFilters` base mirrors `PlaceObject` 1:1. `attributes: PlaceAttributes` nests correctly. `RecallFilters` keeps the structure pulled forward in feature 027; `ConsultFilters` adds discovery-specific fields (`radius_m`, `search_location_name`, `discovery_filters`) without reshaping the base. |
| ADR-057 (confidence bands) | **PASS** | Save tool's user-visible summary maps `ExtractPlaceItem.status` (`saved` / `needs_review` / `duplicate`) to the human line; band interpretation matches the 0.30/0.70 gate. No reinterpretation. |
| ADR-058 (RankingService deleted) | **PASS** | Consult continues to return source-ordered candidates; no ranker reintroduced. |
| ADR-060 (recommendation persistence) | **PASS** | `_persist_recommendation` remains inside `ConsultService` (signature change only; body unchanged). |
| ADR-061 (warming-tier blend + chip filtering) | **PASS** | Warming-blend and active-tier chip filtering logic stay inside `ConsultService`. `signal_tier` remains a call-site argument. Chip-filter taste-service read is the ONE remaining taste-service dependency on the consult main path, explicitly carved out by spec FR-006. |
| ADR-062 (LangGraph StateGraph for agent) | **PASS** | M4/M5/M6 slice of ADR-062's rollout. `ToolNode` structure, `runtime.state` injection pattern, tool→tool data via state, `user_id`/`location` hidden from LLM schema — all match ADR-062 requirements. |
| ADR-063 (two-level ExtractPlaceResponse status) | **PASS** | Save tool consumes the v2 envelope (`status` at pipeline level, `results[].status` at per-place level) as-is from 027's schema. No schema change in this feature. |

**Gate verdict**: PASS. No violations. No new ADR required.

**Complexity Tracking**: Empty (no deviations to justify).

## Project Structure

### Documentation (this feature)

```text
specs/028-agent-tools-wiring/
├── plan.md              # This file (/speckit.plan output)
├── spec.md              # Feature specification (3 clarifications resolved)
├── research.md          # Phase 0 output — dependency + approach research
├── data-model.md        # Phase 1 output — Pydantic / TypedDict shapes
├── quickstart.md        # Phase 1 output — local verification walkthrough
├── contracts/           # Phase 1 output — API + internal contracts
│   ├── chat_response_agent.openapi.yaml    # New type="agent" response shape
│   ├── place_filters.schema.yaml           # PlaceFilters / RecallFilters / ConsultFilters
│   ├── consult_service_signature.md        # New ConsultService.consult() contract
│   ├── tool_schemas.md                     # Three tool input schemas (recall/save/consult)
│   └── agent_dispatch.md                   # ChatService flag-fork + _run_agent contract
├── checklists/
│   └── requirements.md  # Already written by /speckit.specify
└── tasks.md             # Written by /speckit.tasks (NOT this command)
```

### Source Code (repository root)

Single-project src layout per ADR-001. This feature touches the following areas:

```text
src/kebi/
├── api/
│   ├── deps.py                              # EDIT — drop IntentParser/UserMemoryService
│   │                                          from get_consult_service (M4); add
│   │                                          get_agent_graph + agent_graph param on
│   │                                          get_chat_service (M6)
│   ├── main.py                              # LIGHT EDIT — startup hook warms the agent
│   │                                          graph via FastAPI lifespan (M6)
│   ├── routes/
│   │   └── chat.py                          # UNCHANGED (behavior via ChatService)
│   └── schemas/
│       ├── chat.py                          # EDIT — ChatResponse.type → Literal with
│       │                                      "agent" added; keep data: dict[str, Any]
│       └── consult.py                       # EDIT — drop reasoning_steps field from
│                                              ConsultResponse (plan-doc revision to M4:
│                                              steps now delivered via emit callback, not
│                                              bundled into the response)
├── core/
│   ├── agent/
│   │   ├── state.py                         # UNCHANGED (from 027)
│   │   ├── reasoning.py                     # EDIT — add duration_ms: float | None
│   │   │                                      field to ReasoningStep (plan-doc revision;
│   │   │                                      structured-logging standard)
│   │   ├── invocation.py                    # UNCHANGED (from 027)
│   │   ├── graph.py                         # EDIT — make_agent_node extended to emit
│   │   │                                      one agent.tool_decision user-visible step
│   │   │                                      per LLM call (M5 addition)
│   │   ├── checkpointer.py                  # UNCHANGED (from 027)
│   │   └── tools/                           # NEW — M5
│   │       ├── __init__.py                  # build_tools(recall, extraction, consult)
│   │       ├── _emit.py                     # NEW — build_emit_closure + append_summary
│   │       │                                  shared helpers (fan-out: collected list +
│   │       │                                  langgraph.config.get_stream_writer())
│   │       ├── recall_tool.py               # RecallToolInput + build_recall_tool +
│   │       │                                  _recall_summary helper
│   │       ├── save_tool.py                 # SaveToolInput + build_save_tool +
│   │       │                                  _save_summary helper
│   │       └── consult_tool.py              # ConsultToolInput + build_consult_tool +
│   │                                          _consult_summary helper
│   ├── emit.py                              # NEW — EmitFn(Protocol) with
│   │                                          __call__(step, summary, duration_ms=None) -> None
│   │                                          (Protocol not Callable alias, M4 infrastructure)
│   ├── chat/
│   │   └── service.py                       # EDIT — ChatService.run flag fork; add
│   │                                          _run_agent; update _dispatch consult branch
│   │                                          to load saved_places inline and build
│   │                                          ConsultFilters (M4 scaffolding); keep
│   │                                          _run_legacy path identical (M6)
│   ├── consult/
│   │   └── service.py                       # MAJOR EDIT — new signature (adds emit),
│   │                                          drop IntentParser/memory/taste-main-path;
│   │                                          keep chip filtering (M4). Replace
│   │                                          internal ReasoningStep construction with
│   │                                          emit(step, summary) calls. Drop
│   │                                          reasoning_steps from _persist_recommendation
│   │                                          payload. Update "6-step pipeline" docstring.
│   ├── places/
│   │   └── filters.py                       # NEW — PlaceFilters / ConsultFilters base types
│   │                                          (RecallFilters already exists in core/recall/
│   │                                          types.py; that module will import PlaceFilters
│   │                                          and extend it — keeps backwards compatibility
│   │                                          with today's RecallFilters import site).
│   ├── recall/
│   │   ├── types.py                         # EDIT — RecallFilters extends PlaceFilters
│   │   │                                      (keep dataclass vs Pydantic decision — see
│   │   │                                      research.md item 1)
│   │   └── service.py                       # EDIT — run() gains emit: EmitFn | None
│   │                                          parameter; emits recall.mode + recall.result
│   └── extraction/
│       └── service.py                       # EDIT — run() gains emit: EmitFn | None
│                                              parameter; emits save.parse_input,
│                                              save.enrich, optional save.deep_enrichment,
│                                              save.validate, save.persist
├── db/
│   └── repositories/
│       └── recall_repository.py             # EDIT (light) — _build_where_clause already
│                                              walks nested attributes (027 M4 pull-forward);
│                                              formalize PlaceFilters base usage + audit for
│                                              any stale flat-attribute reads
│
config/
├── app.yaml                                 # UNCHANGED (agent: block from 027 stays;
│                                              enabled: false in this feature)
└── prompts/
    └── agent.txt                            # EDIT — add "one tool call per response"
                                                instruction (plan-doc revision, primary
                                                mitigation for the parallel-tool-call
                                                race on AgentState.reasoning_steps)
│
docs/
├── api-contract.md                          # EDIT — document new ChatResponse.type="agent"
│                                              value + reasoning_steps shape on data (M6)
└── decisions.md                             # UNCHANGED (no new ADR this feature)
│
kebi-config/bruno/                         # EDIT (external repo path) — one new .bru
                                              example for the agent-path response

tests/
├── api/
│   ├── schemas/
│   │   └── test_chat.py                     # EDIT — assert Literal on type; "agent" accepted
│   └── routes/
│       └── test_chat.py                     # EDIT — flag-off path identical; flag-on
│                                              end-to-end test (mocked LLM + mocked tools)
├── core/
│   ├── agent/
│   │   ├── test_agent_graph_chain.py        # NEW — recall→consult chain with mocked LLM,
│   │   │                                      asserts last_recall_results flows via state
│   │   │                                      and saved_places never appears in LLM-visible
│   │   │                                      consult args
│   │   ├── test_recall_reset_between_turns.py  # NEW — two-turn flow; assert transient
│   │   │                                         fields reset + messages accumulate
│   │   ├── test_reasoning_visibility.py    # NEW — full turn → JSON payload contains only
│   │   │                                      user-visible steps (3 types)
│   │   ├── test_reasoning_invariants.py    # NEW — four catalog invariants across the 8
│   │   │                                      worked examples in the plan doc
│   │   ├── test_agent_decision_truncation.py  # NEW — 500-char content → summary ≤ 200 chars
│   │   ├── test_agent_decision_fallback.py  # NEW — empty content → synthesized line
│   │   └── tools/
│   │       ├── test_recall_tool.py         # NEW — schema shape, state-reading, summary
│   │       ├── test_save_tool.py           # NEW — inline-await, per-outcome summary
│   │       ├── test_consult_tool.py        # NEW — state-read of last_recall_results,
│   │       │                                  schema doesn't expose saved_places
│   │       └── test_tool_summary_narration.py  # NEW — parametrized across outcome shapes
│   ├── chat/
│   │   └── test_service.py                  # EDIT — flag-off regression; new
│   │                                          test_run_agent_path (mocked graph)
│   ├── consult/
│   │   └── test_service.py                  # REWRITE — pre-built saved_places + filters
│   │                                          fixtures; delete memory/taste main-path asserts;
│   │                                          chip filtering preserved
│   ├── places/
│   │   └── test_filters.py                  # NEW — PlaceFilters base; Consult/Recall extend
│   └── recall/
│       └── test_service.py                  # EDIT — updated fixtures (attrs already nested
│                                              from 027; this milestone formalizes the base)
└── db/
    └── repositories/
        └── test_recall_repository.py        # EDIT — WHERE-clause audit against PlaceFilters
```

**Structure Decision**: Single-project src layout per ADR-001. `core/agent/tools/` is the new submodule; every other change edits an existing file. Test layout mirrors src per ADR-004. No monorepo split, no new deployment surface.

## Complexity Tracking

*Empty — Constitution Check passed with no violations to justify.*

## Phase 0 — Outline & Research

Output: [research.md](./research.md). Research decisions this feature must resolve before writing code:

1. **PlaceFilters base type — dataclass vs Pydantic BaseModel.** `RecallFilters` is currently a `dataclass` (`core/recall/types.py`). `ConsultFilters` needs to be exposed as a tool input field schema in M5, which requires Pydantic (LangChain `@tool` `args_schema` requires Pydantic). Option A: make the shared `PlaceFilters` a Pydantic `BaseModel`, migrate `RecallFilters` from dataclass to Pydantic (caller-side change; only a handful of internal construction sites). Option B: keep `RecallFilters` as a dataclass, duplicate `PlaceFilters`/`ConsultFilters` as Pydantic, and live with the asymmetry. Decide on **Option A** (consistent base, single source of truth, ADR-017 alignment — Pydantic everywhere on boundaries). Document the migration of `RecallFilters` and the impact on `recall_repository._build_where_clause` (pure read; no serialization change).

2. **Closure-based DI for tools (Option A in plan doc) — concrete signature for `@tool`-decorated async callables.** The plan references `build_recall_tool(service) -> Tool` pattern with `ToolRuntime` injection. Research: (a) the LangGraph ^0.3 `ToolRuntime` import path (`from langgraph.runtime import ToolRuntime`) and its state-access API (`runtime.state` / `runtime.tool_call_id`); (b) whether `@tool`-decorated closures interact correctly with LangGraph's `ToolNode` (must accept `runtime` as a keyword arg and not expose it in `args_schema`); (c) whether `tool_call_id` is required for `ToolMessage` construction in the `Command` returned by each tool. Confirm the recipe works with a minimal integration test before scaling to three tools.

3. **LLM injection for the agent graph — orchestrator binding site.** `build_graph(llm, tools, checkpointer)` expects a callable with `.bind_tools(tools).ainvoke(messages)`. Research: (a) `get_llm("orchestrator")` returns a `ChatAnthropic` (langchain-anthropic ^0.3) — confirm `bind_tools` works as expected and returns a runnable with `ainvoke`; (b) Langfuse callback wiring — whether the handler is attached by the provider factory or needs explicit config passthrough in `graph.ainvoke(config={"callbacks": [...]})`. Decide on the single attachment point and document.

4. **FastAPI `lifespan` hook for graph warm-up.** Graph is constructed once at startup regardless of flag. The constitution's ADR-021 establishes the "compile graph at startup" pattern for the old consult StateGraph. This feature replicates that pattern for the new agent graph via FastAPI's `@asynccontextmanager` `lifespan`. Research: (a) the current `api/main.py` startup/shutdown surface — does it already use `lifespan` or legacy `@app.on_event`? (b) where to store the compiled graph so the dependency `get_agent_graph` can return it without re-constructing (app-state attribute on `request.app.state.agent_graph` is the canonical FastAPI pattern). Document the exact hook placement.

5. **`AsyncPostgresSaver` lifecycle.** 027's `build_checkpointer` returns a saver whose `from_conn_string` is an async context manager that was entered via `__aenter__()` but intentionally not exited. Research: (a) whether this leaks a connection at shutdown (acceptable given long-lived saver; document explicitly); (b) whether FastAPI's `lifespan` teardown should call `__aexit__` or just let the process exit. Decide: let it leak on process exit (consistent with 027; safer than trying to coordinate teardown with in-flight requests).

6. **`ChatResponse.type` Literal tightening.** Today `api/schemas/chat.py::ChatResponse.type: str` with a docstring listing the allowed values. Per spec clarification Q1, introduce the new `"agent"` value. Research: (a) tightening `type: str` to `type: Literal["extract-place", "consult", "recall", "assistant", "clarification", "error", "agent"]` — does Pydantic reject existing consumers' payloads (product repo sends requests, not responses — so no); (b) where the type value is constructed today (every branch in `ChatService._dispatch` explicitly sets it) — confirm no silent-`type="something-else"` paths exist. Decide: tighten to Literal, add `"agent"`.

7. **Tool docstring-as-contract — verbatim from plan doc.** The plan's M5 section provides concrete docstrings for `recall_tool`, `save_tool`, `consult_tool` with query-rewriting examples. Research: (a) whether LangChain's `@tool` decorator preserves docstrings to the rendered tool description Sonnet sees, (b) whether the `args_schema` field descriptions from `Field(description=...)` also reach Sonnet. Decide: use both — docstring for tool-level behavior, field descriptions for per-arg guidance. Copy the plan-doc docstrings verbatim.

8. **Reasoning-step emission pattern inside tools — `Command(update=...)` vs return-dict.** The plan shows tool bodies returning `Command(update={...})` with `last_recall_results`, `reasoning_steps`, `messages` keys. Research: (a) LangGraph ^0.3's `Command` import path (`from langgraph.types import Command`), (b) whether `messages` in the `Command` update needs a `ToolMessage` with `tool_call_id=runtime.tool_call_id`, (c) whether returning `Command` vs a plain dict affects how `ToolNode` wires the update. Decide: `Command(update=...)` with an explicit `ToolMessage` is the pattern for all three tools.

9. **Tracing on the agent path — Langfuse span structure.** Every LLM call and every tool invocation must appear as a traceable span (FR-032, SC-010). Research: whether the existing `TracingClient` protocol (upgraded in 027 to use `start_observation` + `update_trace`) works through `graph.ainvoke`, or whether we need to attach Langfuse callbacks via `RunnableConfig` at the call site. Decide the exact `config` shape passed to `graph.ainvoke` for trace-id + user-id propagation.

10. **FakeChatModel for agent-node tests.** Tests for `_run_agent`, recall→consult chain, decision truncation, and decision fallback need an LLM stub that implements `bind_tools(tools).ainvoke(messages) -> AIMessage`. Research: whether LangChain provides a `FakeChatModel` / `GenericFakeChatModel` usable out of the box, or whether a minimal hand-stubbed class is cleaner. Decide: hand-stubbed class (one file, ~30 lines) under `tests/core/agent/_fakes.py` — controllable per-test, no LangChain implementation coupling.

**Output**: `research.md` with one Decision/Rationale/Alternatives entry per item above. All NEEDS CLARIFICATION resolved (none present in Technical Context).

## Phase 1 — Design & Contracts

Prerequisites: `research.md` complete. Outputs below.

### 1. Data model — `data-model.md`

Entities, fields, constraints. Covers:

- **`PlaceFilters`** (Pydantic `BaseModel`, new, in `core/places/filters.py`) — mirrors `PlaceObject` 1:1 per ADR-056. Fields: `place_type: PlaceType | None`, `subcategory: str | None`, `tags_include: list[str] | None`, `attributes: PlaceAttributes | None`, `source: PlaceSource | None`. All optional. No validators beyond the Pydantic types themselves.
- **`RecallFilters`** (Pydantic `BaseModel`, rewritten in `core/recall/types.py`) — extends `PlaceFilters` with retrieval-specific fields: `max_distance_km: float | None`, `created_after: datetime | None`, `created_before: datetime | None`. Migration from dataclass noted in research.md item 1 — caller sites adapt mechanically.
- **`ConsultFilters`** (Pydantic `BaseModel`, new, in `core/places/filters.py`) — extends `PlaceFilters` with discovery-specific fields: `radius_m: int | None`, `search_location_name: str | None`, `discovery_filters: dict[str, Any] | None`.
- **`RecallToolInput`** (Pydantic, in `core/agent/tools/recall_tool.py`) — `query: str | None` (retrieval phrase or null for filter-only), `filters: RecallFilters | None`, `sort_by: Literal["relevance", "created_at"]` (default `"relevance"`), `limit: int` (default 20, `Field(ge=1, le=50)`). Docstring-as-contract with verbatim plan-doc examples.
- **`SaveToolInput`** (Pydantic) — `raw_input: str` (verbatim echo of the user's message — URL or free text). One field.
- **`ConsultToolInput`** (Pydantic) — `query: str`, `filters: ConsultFilters`, `preference_context: str | None`. Does NOT contain `saved_places` (flows via state), `user_id`, or `location`.
- **`ConsultService.consult(...)` new signature** — positional/keyword args: `user_id: str`, `query: str`, `saved_places: list[PlaceObject]`, `filters: ConsultFilters`, `location: Location | None = None`, `preference_context: str | None = None`, `signal_tier: str = "active"`. Returns `ConsultResponse` (existing shape; no change).
- **`ChatResponse.type`** (upgrade in `api/schemas/chat.py`) — from `str` to `Literal["extract-place", "consult", "recall", "assistant", "clarification", "error", "agent"]`. The `"agent"` value is new. Docstring updated.
- **Agent graph dependency (`get_agent_graph`)** — async generator or lifespan-populated callable that returns the compiled `StateGraph` from `request.app.state.agent_graph`. Constructed once per-process at startup.

State transitions — unchanged from feature 027; `build_turn_payload` resets transient fields, `messages` append-via-reducer, `reasoning_steps` plain-overwrite + tool-side append. Checkpointer thread key is `user_id`.

### 2. Contracts — `contracts/`

Five contract artifacts:

**`contracts/chat_response_agent.openapi.yaml`** — OpenAPI fragment for the `type="agent"` response. Shape: `type: "agent"`, `message: str` (agent's final `AIMessage.content`), `data: { reasoning_steps: list[ReasoningStep] }` (only `visibility="user"` steps survive the filter). References `ReasoningStep` schema (re-exported from `core/agent/reasoning.py` → `api/schemas/consult.py`). Documents the three allowed user-visible step types: `agent.tool_decision`, `tool.summary`, `fallback`.

**`contracts/place_filters.schema.yaml`** — JSON/YAML schema for `PlaceFilters`, `RecallFilters`, `ConsultFilters`. Single file so the tool input schemas can link out to one canonical definition. Shows the extension relationship (`allOf`-style) and the disjoint extension fields for recall vs consult.

**`contracts/consult_service_signature.md`** — Internal contract for `ConsultService.consult(...)`. Declares: (1) `saved_places` is required and must be pre-loaded by the caller; (2) the service no longer performs intent parsing, memory load, or main-path taste-profile load; (3) chip filtering still requires a taste-service reference (injected via constructor as today); (4) warming-blend + chip filtering behavior is preserved; (5) `_persist_recommendation` is still called before returning. Error semantics: `NoMatchesError` unchanged; no new exceptions.

**`contracts/tool_schemas.md`** — The three tool input schemas in one document, each with: JSON-schema rendering, Sonnet-facing docstring (verbatim from plan doc), per-field `description` text, and the assertion that `user_id` / `location` / `saved_places` are ABSENT from each respective schema (SC-008 testable).

**`contracts/agent_dispatch.md`** — Internal contract for `ChatService` on the agent path. Declares: (1) flag read is per-request via `self._config.agent.enabled`; (2) flag-off invokes `_run_legacy` (existing classify_intent + dispatch, unchanged); (3) flag-on invokes `_run_agent`: load summaries → `build_turn_payload` → `graph.ainvoke(payload, config={"configurable": {"thread_id": user_id}, "callbacks": [<langfuse>]})` → filter reasoning_steps to `visibility="user"` → map to `ChatResponse(type="agent", message=<last AIMessage.content>, data={"reasoning_steps": [...]})`. Also declares the edge-case behaviors: no AIMessage returned → fallback node already wrote one; graph error before entry → 500.

### 3. Quickstart — `quickstart.md`

Local verification walkthrough (mirrors 027's structure, scoped to the new behaviors):

1. Checkout the branch; `poetry install` (no new deps; confirms 027's locks still apply).
2. `docker compose up -d` — start Postgres + Redis.
3. `poetry run pytest tests/core/places/test_filters.py tests/core/recall tests/core/consult tests/db/repositories/test_recall_repository.py` — verify M4 refactor (`PlaceFilters` base, `RecallFilters`/`ConsultFilters` extends, WHERE-clause audit, ConsultService new signature).
4. `poetry run pytest tests/core/agent/tools/` — verify M5 tool wrappers (schemas hide `user_id`/`location`/`saved_places`; summary helpers narrate outcomes correctly).
5. `poetry run pytest tests/core/agent/test_agent_graph_chain.py tests/core/agent/test_recall_reset_between_turns.py tests/core/agent/test_reasoning_visibility.py tests/core/agent/test_reasoning_invariants.py tests/core/agent/test_agent_decision_truncation.py tests/core/agent/test_agent_decision_fallback.py` — verify M5+M6 integration (recall→consult handoff, per-turn reset, visibility filter, invariants, truncation, fallback).
6. `poetry run pytest tests/core/chat tests/api/routes/test_chat.py tests/api/schemas/test_chat.py` — verify M6 wiring (flag-off regression, flag-on path with mocked LLM+graph, Literal tightening).
7. `poetry run python -c "from kebi.core.config import get_config; c = get_config(); print(c.agent.enabled)"` → prints `False`.
8. Start uvicorn with `config.agent.enabled=false`: `poetry run uvicorn kebi.api.main:app --reload`. `POST /v1/chat` with `{"user_id": "u1", "message": "find me a ramen spot"}` → `type in {"consult", "recall", "assistant"}` (legacy path). No regression.
9. Flip `config/app.yaml` locally: `agent.enabled: true`. Restart uvicorn. Same `POST /v1/chat` → `type="agent"`, `message` from Sonnet, `data.reasoning_steps` populated with user-visible step types only.
10. Agent-path two-turn smoke: Turn 1 `"show me my saved coffee shops"` → `type="agent"`, recall tool called, summary lists count. Turn 2 (same user_id, new message) `"is tipping expected in Japan?"` → `type="agent"`, direct response, exactly one user-visible step (`agent.tool_decision`).
11. `poetry run ruff check src/ tests/ && poetry run ruff format --check src/ tests/ && poetry run mypy src/` — all green.

### 4. Agent context update

Run `.specify/scripts/bash/update-agent-context.sh claude` after Phase 1 artifacts are on disk. Expected updates to `CLAUDE.md` Recent Changes + Active Technologies:

- `PlaceFilters` / `ConsultFilters` added (new shared filter base type in `core/places/filters.py`)
- `RecallFilters` migrated from dataclass → Pydantic extension of `PlaceFilters`
- `ConsultService.consult()` signature simplified (drops IntentParser / memory / taste-main-path; chip filtering stays)
- `core/agent/tools/` module added (three tool wrappers + shared summary helpers)
- `ChatService.run` flag-fork; agent path active when `config.agent.enabled=true`
- `ChatResponse.type` tightened to `Literal` + new `"agent"` value (additive)
- FastAPI `lifespan` warms the compiled agent graph once per process
- `get_agent_graph` FastAPI dependency replaces per-request graph construction

Preserves manual additions between markers per the script's contract.

### Post-Phase-1 Constitution Re-check

After Phase 1 artifacts are written, re-evaluate the constitution table. Expected outcome: still PASS — Phase 1 adds no new architectural surface beyond what Phase 0 already resolved. Document the re-check outcome at the bottom of `research.md`.

## Out-of-Band Deliverables (not Phase 0/1 artifacts, but required by the feature)

These land in normal source code during `/speckit.implement`, not as planning artifacts:

- **`docs/api-contract.md`** updated — document the new `type="agent"` response shape, the `data.reasoning_steps` key, and the three user-visible step types. Note the flag-off default means existing consumers observe no change until M10.
- **Bruno collection** (`kebi-config/bruno/`) — one new `.bru` example for an agent-path response (flag-on). Documents the contract for the product-repo team whenever they begin consuming the new type.
- **Product-repo coordination** — FR-036. Since the flag is off by default, the product repo does not require a code change for this feature's deploy. A heads-up note in the PR description suffices; the actual TypeScript schema update happens before M10's flag flip.

## Stop & Report

This command stops here. `tasks.md` is produced by `/speckit.tasks`.
