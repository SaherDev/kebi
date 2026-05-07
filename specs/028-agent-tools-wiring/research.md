# Phase 0 Research — Agent Tools & Chat Wiring (028-agent-tools-wiring)

Twelve research items resolved. Items 10 (`EmitFn` primitive callback pattern) and 12 (parallel-tool-call caveat + prompt edit) were added after the plan doc (`docs/plans/2026-04-21-agent-tool-migration.md`) was revised mid-planning. Item 10 was further amended when the plan added `duration_ms` to `ReasoningStep` and converted `EmitFn` from a `Callable` alias to a `Protocol`. Everything else was resolved pre-Phase-0. Every Decision is load-bearing for Phase 1 / `/speckit.implement`.

---

## 1. `PlaceFilters` base type — dataclass vs Pydantic `BaseModel`

**Decision**: Pydantic `BaseModel`. Migrate `RecallFilters` from `dataclass` → `BaseModel` extending `PlaceFilters`. Add `ConsultFilters` as a sibling `BaseModel` extension.

**Rationale**:
- LangChain's `@tool(args_schema=...)` requires the argument schema to be a Pydantic `BaseModel` — dataclasses are not accepted as `args_schema`. M5's `ConsultToolInput` has `filters: ConsultFilters`, so the filter type must be Pydantic.
- Constitution ADR-017 mandates Pydantic for all function/module boundaries. The current dataclass `RecallFilters` predates the extension of this rule to filter types; formalizing it now aligns with the constitution.
- Single source of truth: one `PlaceFilters` base reused by recall and consult, mirroring `PlaceObject` 1:1 (ADR-056). Parallel dataclass + Pydantic types would divergence-risk over time.
- `recall_repository._build_where_clause` walks attribute paths on the filter object — Pydantic vs dataclass is irrelevant to dotted-path access (`.attributes.cuisine`), so the repository change is type-annotation only.

**Caller-site impact**: feature 027 already nested the attribute-level fields under `filters.attributes`; the flat `cuisine`/`price_hint`/`neighborhood` construction sites that existed in feature 022 are already gone. Only the import source changes (`RecallFilters` stays in `core/recall/types.py`, imports + extends `PlaceFilters` from `core/places/filters.py`). Test fixtures that build `RecallFilters(...)` keyword-only keep working — Pydantic and dataclass share that constructor surface.

**Alternatives considered**:
- *Keep `RecallFilters` as dataclass, make `PlaceFilters`/`ConsultFilters` Pydantic, live with the asymmetry*: rejected — the shared base loses its purpose if the extensions diverge by paradigm.
- *Make `PlaceFilters` a dataclass and wrap it in a Pydantic adapter at the tool boundary*: rejected — redundant indirection, doubles the boundary surface.

---

## 2. Tool runtime-state access pattern — **`Annotated[..., InjectedState]`**, not `ToolRuntime`

**Decision**: Use LangGraph 0.3's `Annotated[AgentState, InjectedState]` pattern (from `langgraph.prebuilt`) for runtime state access. Use `Annotated[str, InjectedToolCallId]` (from `langchain_core.tools`) for the tool-call-id needed in `ToolMessage` construction. `langgraph.runtime.ToolRuntime` (the pattern shown in the plan doc) does NOT exist in LangGraph 0.3.34 — it is a newer API (LangGraph 0.4+).

**Verified**:
```
$ poetry run python -c "from langgraph.runtime import ToolRuntime"
ModuleNotFoundError: No module named 'langgraph.runtime'

$ poetry run python -c "from langgraph.prebuilt import InjectedState; print('ok')"
ok

$ poetry run python -c "from langchain_core.tools import InjectedToolCallId; print('ok')"
ok
```

Smoke test confirmed that an `@tool(args_schema=...)`-decorated async function with an `Annotated[dict, InjectedState]` parameter renders only the `args_schema` fields in `tool.args_schema.model_json_schema().properties` — the injected state is hidden from the LLM-visible schema. This satisfies SC-008.

**Canonical tool signature for this feature**:
```python
from typing import Annotated
from langchain_core.tools import tool, InjectedToolCallId
from langchain_core.messages import ToolMessage
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

@tool("recall", args_schema=RecallToolInput)
async def recall_tool(
    query: str | None,
    filters: RecallFilters | None,
    sort_by: Literal["relevance", "created_at"],
    limit: int,
    state: Annotated[AgentState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """<docstring is the LLM-facing contract>"""
    ...
    return Command(update={
        "last_recall_results": places,
        "reasoning_steps": (state.get("reasoning_steps") or []) + steps,
        "messages": [ToolMessage(content=response.model_dump_json(),
                                 tool_call_id=tool_call_id)],
    })
```

**Rationale**:
- LangGraph 0.3 uses the `Annotated[..., Injected*]` marker pattern borrowed from LangChain Core. The newer `ToolRuntime` object (introduced in 0.4) is a convenience wrapper over the same mechanism; both produce identical LLM-visible args schemas.
- Upgrading LangGraph to 0.4+ mid-feature is out of scope (risk of breaking 027's graph/checkpointer/state integration; no user-facing value). The spec and ADR-062 both reference LangGraph StateGraph — neither mandates a specific minor version.
- The plan doc's `runtime.state` / `runtime.tool_call_id` signatures translate 1:1 to the injected annotations: `state.get(...)` stays the same; `tool_call_id` becomes a separate parameter.

**Alternatives considered**:
- *Upgrade LangGraph to 0.4+ for `ToolRuntime`*: rejected — not required, introduces upgrade risk, not a value-add for this feature.
- *Use `langchain_core.runnables.RunnableConfig` to access state*: rejected — wrong abstraction; `RunnableConfig` is for chain-level callbacks, not per-tool state access.

**Implication for the plan doc**: the tool-body pattern differs from the plan doc's `runtime: ToolRuntime` signature. All three tool wrappers use the two-parameter injection (`state` + `tool_call_id`) instead of a single `runtime` object. Tool-body semantics are identical — same state reads, same Command returns, same ToolMessage construction.

---

## 3. LLM injection for the agent graph — orchestrator binding site

**Decision**: `get_agent_graph` constructs the LLM via `get_llm("orchestrator")` (the existing provider-abstraction call). The returned `ChatAnthropic` instance already has the Langfuse callback handler attached by the provider factory. `build_graph(llm, tools, checkpointer)` then calls `llm.bind_tools(tools)` inside `make_agent_node` — this returns a runnable with `.ainvoke(messages)` that emits tool calls Sonnet selected.

**Rationale**:
- Constitution Principle III (provider abstraction): `get_llm("orchestrator")` is the only sanctioned way to construct an LLM. The logical role `orchestrator` already points to `claude-sonnet-4-6` in `config/app.yaml`.
- Langfuse: the provider factory (`providers/llm.py`) is responsible for attaching the callback handler to every LLM it hands out. Downstream code (including the agent graph) never re-attaches callbacks explicitly — doing so would double-emit spans. LangGraph's `RunnableConfig` plumbing carries the attached handler through `graph.ainvoke`.
- `bind_tools` on `ChatAnthropic`: confirmed supported in `langchain-anthropic ^0.3`. Returns a runnable whose `ainvoke` accepts a list of messages and returns an `AIMessage` with `tool_calls` populated when Sonnet chose to call a tool.

**Trace-propagation**: `graph.ainvoke(payload, config={"configurable": {"thread_id": user_id}, "metadata": {"user_id": user_id, "session_id": request_id}})`. The `metadata` keys ride with every LLM call (LangGraph propagates `RunnableConfig` through all nodes), so the existing `TracingClient` adapter (upgraded in 027 to use `start_observation` + `update_trace`) sees `user_id` and `session_id` on each span automatically.

**Alternatives considered**:
- *Pass callbacks explicitly in `graph.ainvoke(config={"callbacks": [...]})`*: rejected — doubles spans when combined with provider-factory-attached handler.
- *Make `build_graph` accept a provider-role name and construct the LLM internally*: rejected — breaks testability (injecting a fake LLM becomes harder); injection at the dep-wiring layer is cleaner.

---

## 4. FastAPI `lifespan` hook for graph warm-up

**Decision**: Use FastAPI's `lifespan` async context manager. Construct the agent graph once at startup, store on `app.state.agent_graph`, and let `get_agent_graph` dependency return it via `request.app.state.agent_graph`. No teardown required (see item 5).

**Current state of `api/main.py`**: the repo already uses `lifespan` for other startup tasks (PlacesCache, event dispatcher). The hook surface is established; we extend it with graph construction.

**Graph-construction recipe** (runs inside lifespan):
```python
checkpointer = await build_checkpointer()          # from 027
recall = build_recall_service(...)
extraction = build_extraction_service(...)
consult = build_consult_service(...)
tools = build_tools(recall, extraction, consult)   # M5
llm = get_llm("orchestrator")                       # provider abstraction
app.state.agent_graph = build_graph(llm, tools, checkpointer)
```

**Rationale**:
- ADR-021 establishes "compile graph at startup" as the project pattern (for the old consult StateGraph). Applying it to the new agent graph follows precedent.
- Eager construction regardless of flag value: flipping `agent.enabled: false → true` at runtime must be zero-latency (see M10 canary plan). If construction were flag-gated, the first flag-on request would pay the graph-build cost.
- The lifespan hook running fully before the first request arrives is standard FastAPI semantics — `uvicorn` blocks on lifespan startup completion before accepting traffic.

**Startup-failure behavior**: if `DATABASE_URL` is unreachable at startup, `build_checkpointer` fails — the service does not start. Same semantics as feature 027 (the checkpointer was already eager-built there). Acceptable because (a) the service cannot function without its Postgres anyway, and (b) it makes misconfiguration visible at deploy time, not at first-request time.

**Alternatives considered**:
- *Lazy construction on first flag-on request*: rejected — first-request latency spike, harder to debug misconfiguration (surfaces at first call instead of boot).
- *Module-level singleton (`_GRAPH = None; def get(): global _GRAPH; if not _GRAPH: ...`)*: rejected — doesn't fit FastAPI's dependency model; untestable without monkey-patching.

---

## 5. `AsyncPostgresSaver` lifecycle

**Decision**: Enter the context manager with `__aenter__()` at startup and intentionally do NOT call `__aexit__()` at shutdown. Let the process exit release the connection. Consistent with feature 027; no change.

**Rationale**:
- In langgraph-checkpoint-postgres 3.x, `AsyncPostgresSaver.from_conn_string(url)` returns an `AbstractAsyncContextManager[AsyncPostgresSaver]`. Entering yields a saver bound to a live asyncpg connection; exiting closes the connection. For a long-lived application, we want one saver for the lifetime of the process.
- Teardown coordination with in-flight requests is hazardous: `lifespan` shutdown fires BEFORE all in-flight requests finish, so exiting the context manager could sever the saver's connection while a checkpoint write is in progress. The cleaner path is to let the process exit tear everything down.
- "Connection leak on shutdown" is a non-issue for a Railway-deployed service — the OS reaps the connection when the process exits.

**Research action in Phase 0**: none beyond confirming the pattern from 027 still applies. No code or test change.

**Alternatives considered**:
- *Add an `__aexit__` call in `lifespan` teardown*: rejected — race risk during graceful shutdown.
- *Use `AsyncConnectionPool` instead of `from_conn_string`*: rejected for this feature — bigger change, no clear value given 027 already shipped with `from_conn_string`; revisit at M6 canary if connection management becomes a bottleneck.

---

## 6. `ChatResponse.type` Literal tightening

**Decision**: Change `type: str` in `api/schemas/chat.py` to `type: Literal["extract-place", "consult", "recall", "assistant", "clarification", "error", "agent"]`. The `"agent"` value is new (per spec clarification Q1). Update the docstring to list the allowed values.

**Grepped call sites** (to confirm no silent `type="foo"` construction exists outside the enumerated values):
- `core/chat/service.py`: every `return ChatResponse(...)` sets `type` to one of the six existing Literal values. Will add a seventh (`"agent"`) in `_run_agent`.
- `tests/`: a handful of fixtures construct `ChatResponse(type="...")` — all match the Literal. Will audit during implementation.

**Rationale**:
- The spec clarification Q1 explicitly says "additive to the existing type Literal" — but today the field is typed `str` with a docstring listing the allowed values. Tightening to `Literal` makes the contract machine-checkable (mypy catches drift) and satisfies FR-028 verbatim ("new value added to the existing `ChatResponse.type` Literal set").
- Product-repo impact is zero: the product repo sends `ChatRequest` (not `ChatResponse`) to this service, and reads response `type` as a string on its side. A tighter type here has no wire-format effect.

**Alternatives considered**:
- *Keep `type: str` and just add the new string value*: rejected — loses the machine-checkable contract; drift becomes invisible.
- *Introduce a separate `AgentChatResponse` subclass*: rejected — forks the shape unnecessarily; legacy consumers still need to parse `type` to branch.

---

## 7. Tool docstring-as-contract — verbatim from plan doc

**Decision**: Use the plan-doc docstrings verbatim for `recall_tool`, `consult_tool`, and `save_tool`. Use Pydantic `Field(description=...)` for per-argument descriptions on each `*ToolInput` model (also verbatim from the plan doc for the examples).

**Verification**: LangChain's `@tool` decorator (in `langchain_core.tools`) reads `func.__doc__` and stores it on `tool.description`. That field is what Sonnet sees in the `tools` array passed to `llm.bind_tools(tools)`. Smoke test confirmed.

Similarly, `args_schema.model_json_schema()` exposes each field's `description` (from `Field(description=...)`). LangChain propagates these into the `inputSchema.properties.<field>.description` that Sonnet reads alongside the tool-level description.

**Rationale**:
- The plan-doc docstrings encode the per-tool query-rewriting rules Sonnet must follow. Hand-translating or paraphrasing risks semantic drift.
- The examples in each field's `description` (e.g. `"'find me a good ramen spot nearby' -> query='ramen restaurant'"`) are the primary mechanism teaching Sonnet how to fill the args — they must be concrete and preserved verbatim.

**Alternatives considered**:
- *Centralize all tool guidance in the system prompt*: rejected — bloats the prompt, couples tool-specific rules to the persona prompt, breaks the plan's "docstring is the contract" decision.
- *Write shorter docstrings*: rejected — the worked examples from the plan doc are load-bearing for tool-arg quality.

---

## 8. Reasoning-step emission — `Command(update=...)` with explicit `ToolMessage`

**Decision**: Each tool body returns `Command(update={"last_recall_results": ..., "reasoning_steps": ..., "messages": [ToolMessage(content=..., tool_call_id=tool_call_id)]})`. The `messages` key MUST include a `ToolMessage` whose `tool_call_id` matches the injected `tool_call_id` parameter — otherwise LangGraph raises "no ToolMessage for tool_call_id X".

**Verified imports** (LangGraph 0.3.34):
- `from langgraph.types import Command` — OK
- `from langchain_core.messages import ToolMessage` — OK
- `from langchain_core.tools import InjectedToolCallId` — OK

**Rationale**:
- `Command(update=...)` is LangGraph's sanctioned way for a tool to write to state beyond just appending a `ToolMessage`. Returning a plain dict would not update `last_recall_results` or `reasoning_steps` in the graph state.
- `ToolMessage` with a matching `tool_call_id` is non-optional — LangGraph's `ToolNode` enforces the tool-call-id handshake.

**State-append semantics** (no reducer on `reasoning_steps`):
- Per feature 027's state design, `reasoning_steps` has plain-overwrite semantics (no reducer). Tools must concatenate (`(state.get("reasoning_steps") or []) + new_steps`) and return the full list in the `Command` — NOT a delta.
- `last_recall_results` also has plain-overwrite semantics. Tools overwrite it directly.
- `messages` has `add_messages` reducer (from 027) — tools return only new messages; the reducer appends.

**Alternatives considered**:
- *Plain-dict return (`return {"messages": [...]}`)*: rejected — cannot update non-`messages` state keys reliably with plain dict return + no reducer.
- *Introduce an `add_reasoning_steps` reducer*: rejected — 027 explicitly chose plain-overwrite for reset semantics; reintroducing a reducer would force a sentinel value for reset, regressing the design.

---

## 9. Tracing on the agent path — Langfuse span structure

**Decision**: No explicit callback attachment at `graph.ainvoke`. Rely on the provider-factory-attached Langfuse handler. Propagate `user_id` and an optional `session_id` via `config={"metadata": {...}, "configurable": {"thread_id": user_id}}` passed to `graph.ainvoke`. The existing `TracingClient` (feature 027 upgraded it to `start_observation` + `update_trace`) consumes those metadata keys during its `update_trace` call at LLM-invocation time.

**Rationale**:
- Constitution Principle II (ADR-025): Langfuse handler attached at the provider-factory layer. The agent graph, tool wrappers, and `_run_agent` never re-attach — doing so would create duplicate spans.
- `thread_id` in `configurable` is the checkpointer thread key (required by LangGraph for checkpointing to work). It also surfaces as a trace attribute at Langfuse, which gives us free per-user trace grouping.
- FR-032 mandates trace coverage of every LLM call and every tool invocation. Tool calls surface as `AIMessage` (LLM span) + `ToolMessage` (tool span) via LangGraph's default callback plumbing — no extra work needed.

**Smoke check in M6 implementation**: after the agent path is wired, a synthetic `POST /v1/chat` with flag-on should produce a Langfuse trace containing (a) one orchestrator LLM span per `agent` node execution, (b) one tool span per tool call, (c) the `user_id` and `thread_id` attributes on the parent trace. Verify manually before declaring FR-032 met.

**Alternatives considered**:
- *Attach `LangfuseCallbackHandler` to `graph.ainvoke(config={"callbacks": [...]})` explicitly*: rejected — double-spans.
- *Drop the `metadata` propagation and rely on Langfuse auto-detection*: rejected — loses per-user grouping in the Langfuse UI.

---

## 10. `EmitFn` primitive callback pattern (plan-doc revision)

**Decision**: Introduce `EmitFn` in `src/kebi/core/emit.py` as a `typing.Protocol` (not a plain `Callable` alias) whose `__call__` accepts `step: str`, `summary: str`, and an optional `duration_ms: float | None = None`. Each of `RecallService.run`, `ConsultService.consult`, and `ExtractionService.run` gains an optional `emit: EmitFn | None = None` parameter. Services call `emit(step_name, summary)` at each pipeline boundary with primitive string tuples, or `emit(step, summary, duration_ms=elapsed)` when they have measured the work directly. Either form is valid. Services never construct `ReasoningStep` objects and never import from `core/agent/`. Tool wrappers own the agent-layer fields (`source`, `tool_name`, `visibility`, `timestamp`, `duration_ms`) via a shared closure pattern in `src/kebi/core/agent/tools/_emit.py` (`build_emit_closure` + `append_summary` helpers). The closure fans out to both (a) a collected list consumed by `Command.update["reasoning_steps"]` at node return and (b) `langgraph.config.get_stream_writer()` for live SSE frames.

**Rationale**:
- **Services emit; wrappers frame.** Services carry domain semantics (what pipeline stage completed, what the headline number is, optionally how long it took). Agent-layer fields (source, tool_name, visibility, timestamp, optionally duration when the service didn't measure) are concerns of the agent runtime, not the domain. Keeping them split means services stay reusable outside the agent (legacy flag-off path, future CLI consumers, future eval harnesses) without dragging the `ReasoningStep` import.
- **Protocol over Callable alias.** `typing.Protocol` is required (not just stylistic) because `duration_ms` has a default — `Callable[[str, str, float | None], None]` can't express that. The Protocol form is structurally typed, so spy callables, production closures, and no-op defaults all satisfy it without explicit inheritance.
- **`duration_ms` on every step.** Aligns with structured-logging standards. Per-step elapsed time is the primary debugging signal for evals and perf regressions. Populated one of two ways: (a) the service passes it explicitly when it wrapped the operation in its own timer (e.g., a Google Places call, a pgvector query), or (b) the wrapper's emit closure computes it from timestamp deltas (time since the previous emit on the same closure, or since closure build time for the first emit). Always populated in the final persisted step; `None` on input just means "let the wrapper compute it."
- **One fan-out point.** If Langfuse spans, metric counters, or step-field defaults need to change, there's exactly one file to edit (`_emit.py`). Previously the wrapper was building debug steps inline in each `recall_tool.py` / `save_tool.py` / `consult_tool.py` — now they delegate to `build_emit_closure` + `append_summary`.
- **`ConsultResponse.reasoning_steps` is deleted** by this pattern. Steps are delivered live via `emit` during `consult()` execution, not serialized into the response. Downstream impact is minimal: `_persist_recommendation` drops the field from its JSONB payload; existing consult tests that assert on `response.reasoning_steps` are rewritten to spy on `emit`. The flag-off legacy chat dispatch does NOT read `response.reasoning_steps` today, so no user-visible behavior change on that path.
- **Stream writer access.** Research confirmed `langgraph.config.get_stream_writer()` is available in LangGraph 0.3.34 — callable from any node or tool body. The plan-doc's `runtime.stream_writer` pattern translates 1:1 to `get_stream_writer()` with the same semantics (returns `None` when no streaming caller is attached).

**Helper signatures** (the plan doc uses `ToolRuntime`; we substitute `get_stream_writer()` per research item 2 and access `runtime.state` / `runtime.tool_call_id` via the `Annotated[..., Injected*]` parameters):

```python
# src/kebi/core/agent/tools/_emit.py
from datetime import UTC, datetime
from typing import Literal
from langgraph.config import get_stream_writer
from kebi.core.agent.reasoning import ReasoningStep
from kebi.core.emit import EmitFn

ToolName = Literal["recall", "save", "consult"]


def build_emit_closure(
    tool_name: ToolName,
) -> tuple[list[ReasoningStep], EmitFn]:
    """Return (collected, emit) for a tool wrapper.

    `emit(step, summary, duration_ms=None)`:
      - duration_ms defaults to None → closure computes from timestamp delta
        (time since previous emit, or since closure build for the first emit).
      - When the caller passes duration_ms explicitly, that value is used verbatim.
      - Resulting ReasoningStep is appended to `collected` and streamed via
        get_stream_writer() when a caller is streaming; no-op otherwise.
    """
    collected: list[ReasoningStep] = []
    last_ts = datetime.now(UTC)
    writer = get_stream_writer()   # returns None if no caller is streaming

    def emit(step: str, summary: str, duration_ms: float | None = None) -> None:
        nonlocal last_ts
        now = datetime.now(UTC)
        if duration_ms is None:
            duration_ms = (now - last_ts).total_seconds() * 1000.0
        rs = ReasoningStep(
            step=step, summary=summary,
            source="tool", tool_name=tool_name, visibility="debug",
            timestamp=now, duration_ms=duration_ms,
        )
        collected.append(rs)
        if writer is not None:
            writer(rs.model_dump())
        last_ts = now

    return collected, emit


def append_summary(
    collected: list[ReasoningStep],
    tool_name: ToolName,
    summary: str,
) -> None:
    """Append the wrapper's user-visible tool.summary step.

    duration_ms reflects the total tool-invocation elapsed time — from the
    first emit in `collected` to now. When `collected` is empty (no debug
    emits happened before summary), duration_ms is 0.0.
    """
    now = datetime.now(UTC)
    start = collected[0].timestamp if collected else now
    rs = ReasoningStep(
        step="tool.summary", summary=summary,
        source="tool", tool_name=tool_name, visibility="user",
        timestamp=now,
        duration_ms=(now - start).total_seconds() * 1000.0,
    )
    collected.append(rs)
    writer = get_stream_writer()
    if writer is not None:
        writer(rs.model_dump())
```

Note: the helper signatures diverge from the plan-doc version in one place — the plan doc threads `runtime: ToolRuntime` through both helpers for stream_writer access. Our helpers call `get_stream_writer()` directly (LangGraph 0.3.34 pattern; research item 2). Behavior is identical: writer is `None` when no streaming caller is attached, so the `if writer is not None` guard is no-op during non-streaming turns. `duration_ms` computation (nonlocal `last_ts` rebind in emit, `collected[0].timestamp → now` for append_summary) is verbatim from the plan doc.

**Tool-body shape** (after this change, all three wrappers read the same — the body does not change when `duration_ms` is added; it's the closure that handles both cases):
```python
@tool("recall", args_schema=RecallToolInput)
async def recall_tool(
    query, filters, sort_by, limit,
    state: Annotated[AgentState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """..."""
    collected, emit = build_emit_closure("recall")
    response = await service.run(
        query=query, user_id=state["user_id"], filters=filters,
        sort_by=sort_by, location=state.get("location"),
        limit=limit, emit=emit,
    )
    places = [r.place for r in response.results]
    append_summary(collected, "recall", _recall_summary(query, filters, places))
    return Command(update={
        "last_recall_results": places,
        "reasoning_steps": (state.get("reasoning_steps") or []) + collected,
        "messages": [ToolMessage(
            content=response.model_dump_json(),
            tool_call_id=tool_call_id,
        )],
    })
```

**Alternatives considered**:
- *Services construct `ReasoningStep` directly and return them on the response*: rejected — this was the old shape (consult service did this in feature 027); the plan-doc revision explicitly deletes it because (a) response-bundled steps can only be emitted at node boundaries, not during execution, making SSE streaming impossible without a separate callback channel, and (b) coupling domain services to the agent layer's `ReasoningStep` type is a repo-boundary smell.
- *Passing `ToolRuntime` through to `_emit.py` helpers*: rejected — not available on LangGraph 0.3.34 (research item 2). `get_stream_writer()` is the equivalent and is already the canonical way the agent node accesses the writer.
- *A richer emit protocol with structured payload (`emit(step, summary, details)`)*: rejected — services shouldn't know about the structured trace, and the plan-doc mandates primitive strings. If step granularity needs to grow, add more emit call sites, not more emit-arg fields.

**Test implications** (FR-035(h)):
- `tests/core/recall/test_service.py` — add a spy `emit` list, assert `("recall.mode", ...)` and `("recall.result", ...)` are called in order across filter-only / hybrid branches.
- `tests/core/consult/test_service.py` — rewrite tests that previously asserted on `response.reasoning_steps` (e.g., `test_warming_blend_signal`, `test_active_rejected_chip_filter`, `test_active_confirmed_chips_surfaced`) to spy on `emit` and assert the expected `(step, summary)` sequences.
- `tests/core/extraction/test_service.py` — add spy `emit` to the existing inline-await tests; assert `save.parse_input → save.enrich → (optional save.deep_enrichment) → save.validate → save.persist` sequence.
- `tests/core/agent/tools/test_recall_tool.py` / `test_save_tool.py` / `test_consult_tool.py` — verify `collected` grows in the expected order (service emits first, then wrapper appends `tool.summary` last).
- `tests/core/agent/tools/test_emit_helpers.py` (NEW) — unit test `build_emit_closure` and `append_summary`: step construction matches catalog, stream writer fan-out fires when attached, no-op when writer is `None`.

---

## 12. Parallel-tool-call caveat + one-tool-call-per-response prompt edit (plan-doc revision)

**Decision**: Edit `config/prompts/agent.txt` (shipped in 027) to instruct the orchestrator model to emit **one tool call per response** and chain sequentially across turns rather than parallelize within one `AIMessage`. No reducer is added to `AgentState.reasoning_steps`.

**The risk being mitigated**: `AgentState.reasoning_steps` has no reducer by design (feature 027 M3 — plain-overwrite makes the per-turn reset unambiguous). Each tool's `Command.update` sets `reasoning_steps = prior + collected`. If Sonnet emitted two tool calls in a single `AIMessage`, LangGraph's `ToolNode` would run them concurrently — both read `prior` at the same time, each builds its own `prior + collected`, and the last writer wins. The first tool's debug/summary steps get dropped on the floor.

**Why prompt mitigation, not a reducer**:
- The recall → consult design is sequential by construction: consult needs `last_recall_results` populated first, which requires the recall tool's `Command.update` to commit before consult runs. Parallelizing tool calls in one response would break that handoff regardless of the reasoning-steps race.
- A list-merge reducer on `reasoning_steps` would re-ambiguate the per-turn reset (empty list could mean "nothing to add" vs "reset"), forcing a sentinel value and more state-shape complexity. 027 explicitly chose plain-overwrite to avoid exactly this.
- The mitigation is free at prompt-edit time: Sonnet's default tool-calling behavior is already one tool per response in most cases; the instruction makes it explicit and documentable.

**Prompt edit** — a new paragraph under `## Your tools` in `config/prompts/agent.txt` stating roughly: "Emit at most one tool call per response. When a request needs more than one tool (e.g. recall then consult), chain sequentially across turns — call the first tool, wait for its result in the next turn, then call the next tool. Do not emit multiple tool calls in a single response."

**Defensive test**: a companion test (`tests/core/agent/test_one_tool_call_per_response.py` — spy on every `AIMessage` produced by `agent_node` across a canary prompt suite, assert `len(ai_msg.tool_calls) <= 1`) is explicitly scheduled in **M9** per the plan doc, NOT this feature. Feature 028 ships the prompt instruction + documented caveat; the assertion test lands alongside the failure-budget / timeout operationalization work. This is a conscious scope split: the mitigation ships now, the automated guard ships with the rest of the operational hardening.

**Future option (if parallel is ever intentional)**: swap `reasoning_steps` to a list-merge reducer (e.g., `Annotated[list[ReasoningStep], operator.add]`) and move the per-turn reset into a dedicated `session_init` node. Not done here.

**Alternatives considered**:
- *List-merge reducer now*: rejected — complicates per-turn reset, contradicts 027's design, no current driver (one-call-per-response meets every use case in the 8 worked examples).
- *Serialize tool calls in the graph* (force sequential execution even when Sonnet emits multiple): rejected — requires a custom ToolNode or graph rewrites; prompt mitigation is cheaper.
- *Add runtime check that rejects multi-tool AIMessages inside `agent_node`*: rejected for this feature (belongs with M9's failure-budget work); the simpler path is the prompt.

**Test implications** (for this feature only — companion test is M9):
- No new test in this feature.
- Manual verification: run the quickstart's flag-on smoke suite and confirm every `AIMessage` in the Langfuse trace has at most one `tool_calls` entry.

---

## 11. FakeChatModel for agent-node tests — `GenericFakeChatModel` vs hand-stubbed

**Decision**: Use LangChain's `GenericFakeChatModel` from `langchain_core.language_models.fake_chat_models`. Verified available in `langchain-core 0.3.83`. Implements `bind_tools` and `ainvoke` with scripted responses, which is what the agent node needs for deterministic tests.

**Verified**:
```
$ poetry run python -c "from langchain_core.language_models.fake_chat_models import GenericFakeChatModel; print('ok')"
ok
```

**Usage pattern**:
```python
# tests/core/agent/_fakes.py
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

def make_fake_llm(responses: list[AIMessage]) -> GenericFakeChatModel:
    return GenericFakeChatModel(messages=iter(responses))
```

Tests pass pre-scripted `AIMessage` objects that include `tool_calls` for multi-tool-chain tests or plain content for direct-response tests.

**Rationale**:
- Avoids one-off hand-stubbed classes scattered across tests. `GenericFakeChatModel` is designed for this exact use case.
- Supports `bind_tools` (no-op passthrough in the fake), so the agent-node closure `bound = llm.bind_tools(tools)` works without modification.
- Cleaner than mocking: tests read as concrete scenarios (scripted responses), not mock-invocation graphs.

**Alternatives considered**:
- *Hand-stubbed fake LLM class in `tests/core/agent/_fakes.py`*: rejected — LangChain provides a maintained fake; reinventing invites compatibility drift when LangChain upgrades.
- *`unittest.mock.AsyncMock(spec=ChatAnthropic)`*: rejected — brittle to `bind_tools` return type; fails closed when LangChain tightens the `Runnable` protocol.

---

## Post-Phase-1 Constitution Re-check

Phase 1 artifacts written (`data-model.md`, `contracts/chat_response_agent.openapi.yaml`, `contracts/place_filters.schema.yaml`, `contracts/consult_service_signature.md`, `contracts/tool_schemas.md`, `contracts/agent_dispatch.md`, `quickstart.md`). CLAUDE.md Recent Changes + Active Technologies updated with a hand-tightened entry.

Plan-doc revision integrated via research item 10 (EmitFn primitive callback pattern) after Phase 1 artifacts were initially written; data-model.md gained §0 (`EmitFn`), §7a (`RecallService.run` emit), §7b (`ExtractionService.run` emit), §7c (`ConsultResponse` field removal), and the §7 ConsultService signature was updated in place. `contracts/consult_service_signature.md` and `contracts/tool_schemas.md` updated in lockstep. `contracts/agent_dispatch.md` unaffected — the agent-layer response shape (`ChatResponse` with `data.reasoning_steps`) is unchanged; only the consult-service response envelope loses its reasoning-steps field.

Re-evaluating the plan's Constitution Check table (15 principles + binding ADRs):

- **I. Repo Boundary**: PASS — Phase 1 contracts are all AI-side; `chat_response_agent.openapi.yaml` documents a new `type="agent"` value that the product repo consumes only when the flag flips (not this feature).
- **II. ADRs are Constraints**: PASS — no new ADR was introduced by Phase 1. `PlaceFilters` base is documented in `place_filters.schema.yaml` as an extension of ADR-056's `PlaceObject` shape, not a supersession.
- **III. Provider Abstraction**: PASS — `contracts/agent_dispatch.md` wires `get_llm("orchestrator")` at the lifespan layer; no model names hardcoded.
- **IV. Pydantic Everywhere**: PASS — `data-model.md` makes `PlaceFilters`, `RecallFilters`, `ConsultFilters`, and all three `*ToolInput` types Pydantic. Research item 1 formalizes the `RecallFilters` dataclass → Pydantic migration. `AgentState` remains `TypedDict` (LangGraph requirement, prior justification).
- **V. Configuration Rules**: PASS — no new config keys in this feature; `config.agent.*` already shipped with 027.
- **VI. DB Write Ownership**: PASS — no new tables, no Alembic changes.
- **VII. Redis Ownership**: PASS — no new Redis keys, no new Redis owners.
- **VIII. API Contract**: PASS — `chat_response_agent.openapi.yaml` documents the additive `"agent"` value on the response Literal; no new routes. Flag-off preserves existing consumers.
- **IX. Testing**: PASS — `quickstart.md` enumerates the test execution order; FR-035 coverage targets map 1:1 to the new test files under `tests/core/agent/tools/` and `tests/core/agent/`.
- **X. Git & Commits**: PASS — `028-agent-tools-wiring` branch matches the spec-kit convention.
- **ADR-019 (Depends only)**: PASS — `get_agent_graph` dependency in `contracts/agent_dispatch.md` uses `Depends(...)` throughout.
- **ADR-025 (Langfuse on every LLM call)**: PASS — research item 9 confirms the single-attachment-point design and documents the `metadata` propagation.
- **ADR-044 (prompt-injection)**: PASS — agent prompt (from 027) carries the three mitigations; tool docstrings are data, not executable instructions; no new injection surface.
- **ADR-052 (`/v1/chat` unified)**: PASS — no new routes.
- **ADR-056 (PlaceObject unified)**: PASS — `place_filters.schema.yaml` mirrors `PlaceObject` 1:1; the extension types add fields only.
- **ADR-057 (confidence bands)**: PASS — save tool's user-visible summary maps per-place status correctly.
- **ADR-058 (RankingService deleted)**: PASS — consult continues to return source-ordered candidates.
- **ADR-060 (recommendation persistence)**: PASS — `_persist_recommendation` remains inside `ConsultService`.
- **ADR-061 (warming blend + chip filtering)**: PASS — warming-blend logic retained; chip-filter taste-service read explicitly carved out as the ONE remaining dependency on the consult main path.
- **ADR-062 (LangGraph StateGraph for agent)**: PASS — `contracts/tool_schemas.md` documents the LangGraph 0.3 injection pattern; semantics match the ADR. Research item 2 records the plan-doc vs implementation-version translation.
- **ADR-063 (two-level ExtractPlaceResponse)**: PASS — save tool consumes the envelope as-is.

**Gate verdict**: PASS, unchanged from pre-Phase-0.

**Complexity Tracking**: Still empty. No new violations introduced by Phase 1 artifacts.

---

## Summary of deltas from the plan doc

The plan doc (`docs/plans/2026-04-21-agent-tool-migration.md`) assumes LangGraph 0.4+ idioms in three places. This feature uses LangGraph 0.3.34 equivalents:

| Plan-doc idiom | This-feature idiom | Reason |
|----------------|--------------------|--------|
| `runtime: ToolRuntime` parameter on each tool | `state: Annotated[AgentState, InjectedState]` + `tool_call_id: Annotated[str, InjectedToolCallId]` | 0.3 doesn't have `langgraph.runtime.ToolRuntime`; `Annotated[..., Injected*]` is the 0.3 pattern and produces an identical LLM-visible args schema. |
| `runtime.state` | `state` (the injected parameter) | Same data, different binding site. |
| `runtime.tool_call_id` | `tool_call_id` (the injected parameter) | Same data. |
| `runtime.stream_writer` on the tool body and inside `build_emit_closure` / `append_summary` helpers | `langgraph.config.get_stream_writer()` called directly from the helper | 0.3.34 provides `get_stream_writer()` as the canonical way to access the writer from anywhere inside graph execution — tool body or node body. Returns `None` when no streaming caller is attached (identical semantics to `runtime.stream_writer`). |

Semantics, state reads, `Command` returns, `ToolMessage` construction, the three user-visible reasoning steps, **and the `EmitFn` primitive callback pattern (plan-doc revision, research item 10)** are identical. The swaps are ergonomic only; there is no functional behavior change relative to the plan doc's intent.

Every other plan-doc decision (docstring-as-contract, closure-based DI, tool→tool via state, `saved_places` hidden from consult schema, three user-visible step types, eager graph warm-up, flag read per-request, `ConsultResponse.reasoning_steps` field deletion) carries through to this feature verbatim.
