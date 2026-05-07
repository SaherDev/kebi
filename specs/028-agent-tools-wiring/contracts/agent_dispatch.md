# ChatService — flag-fork dispatch contract (M6)

Internal contract for `src/kebi/core/chat/service.py::ChatService` after M6 wiring.

## `ChatService.__init__` — new signature

```python
class ChatService:
    def __init__(
        self,
        extraction_service: ExtractionService,
        consult_service: ConsultService,
        recall_service: RecallService,
        assistant_service: ChatAssistantService,
        intent_parser: IntentParser,
        event_dispatcher: EventDispatcherProtocol,
        memory_service: UserMemoryService,
        taste_service: TasteModelService,
        config: AppConfig,
        agent_graph: Any,                   # CompiledStateGraph
    ) -> None: ...
```

Constructor contract:
- **Kept** from today: `extraction_service`, `consult_service`, `recall_service`, `assistant_service`, `intent_parser`, `event_dispatcher`, `memory_service`.
- **Added**: `taste_service` (needed on the agent path for taste_profile_summary formatting), `config` (needed for `config.agent.enabled` per-request read), `agent_graph` (the compiled graph built at startup).

## `ChatService.run` — flag fork

```python
async def run(self, request: ChatRequest) -> ChatResponse:
    try:
        if self._config.agent.enabled:
            return await self._run_agent(request)
        return await self._run_legacy(request)
    except Exception as exc:
        logger.exception("ChatService.run failed: %s", exc)
        return ChatResponse(
            type="error",
            message="Something went wrong, please try again.",
            data={"detail": str(exc)},
        )
```

Flag read contract:
- Evaluated once per request at the top of `run()`.
- Flag-off → `_run_legacy` (the existing classify_intent + dispatch, unchanged externally).
- Flag-on → `_run_agent` (new).
- Outer try/except is the terminal safety net. Agent-path fallbacks (step budget, error budget) are handled INSIDE the graph by `fallback_node` — those return through the normal path, not through the exception.

## `_run_legacy` — existing behavior preserved

`_run_legacy` is the renamed version of today's `run()` body (classify_intent + event dispatch + `_dispatch`). No semantic change aside from:

1. **Consult branch of `_dispatch`** — now loads saved places inline and builds an empty `ConsultFilters` (per spec clarification Q2 + data-model.md §11). Calls `ConsultService.consult(user_id, query, saved_places, filters, location, preference_context=None, signal_tier="active")` with the new kwargs-only signature.
2. **Other branches** (`extract-place`, `recall`, `assistant`, `clarification`) — unchanged.

The helper `_filters_from_parsed` (pulled forward in 027) is deleted from this file if the consult branch no longer uses it; the recall branch's use of `ParsedIntent` → `RecallFilters` stays.

## `_run_agent` — new path

```python
async def _run_agent(self, request: ChatRequest) -> ChatResponse:
    taste_summary = await self._compose_taste_summary(request.user_id)
    memory_summary = await self._compose_memory_summary(request.user_id)

    payload = build_turn_payload(
        message=request.message,
        user_id=request.user_id,
        taste_profile_summary=taste_summary,
        memory_summary=memory_summary,
        location=request.location.model_dump() if request.location else None,
    )

    graph_config = {
        "configurable": {"thread_id": request.user_id},
        "metadata": {"user_id": request.user_id},
    }
    final_state = await self._agent_graph.ainvoke(payload, config=graph_config)

    ai_message = _last_ai_message(final_state["messages"])
    user_steps = [
        s for s in final_state.get("reasoning_steps", [])
        if s.visibility == "user"
    ]

    return ChatResponse(
        type="agent",
        message=ai_message.content if ai_message else "",
        data={"reasoning_steps": [s.model_dump(mode="json") for s in user_steps]},
    )
```

Where:
- `_compose_taste_summary(user_id) -> str`: calls `self._taste_service.get_taste_profile(user_id)`, handles None, calls `format_summary_for_agent(lines)` on the non-null branch. Returns `""` when the user has no taste profile yet.
- `_compose_memory_summary(user_id) -> str`: calls `self._memory.load_memories(user_id)`, joins with `"\n"`. Returns `""` on empty.
- `_last_ai_message(messages)`: iterates `reversed(messages)`, returns the first `AIMessage` or `None`.

Contract invariants:
- `type="agent"` is always set on this path.
- `data.reasoning_steps` is always present (possibly `[]`).
- `message` is the last `AIMessage.content` from the final state. If no `AIMessage` exists (pathological — fallback node always produces one), `message=""`. The outer try/except would catch a pre-ainvoke failure; the graph guarantees an `AIMessage` on successful exit.

Thread-key contract:
- `thread_id=request.user_id` — the checkpointer keys conversation history by user. Multi-session support (session_id nesting) is not in scope for this feature; revisit if multiple concurrent conversations per user become a product requirement.

Metadata contract:
- `metadata.user_id` rides with every LLM call via LangGraph's `RunnableConfig` propagation. The existing `TracingClient` (feature 027) reads this via `update_trace` automatically — no explicit Langfuse callback attachment at this call site (research.md item 9).

## `PersonalFactsExtracted` event on the agent path

On the flag-off legacy path, `classify_intent` returns a `ClassificationResult` including `personal_facts` that get dispatched via `PersonalFactsExtracted` before `_dispatch`. On the agent path, `classify_intent` is NOT called — the agent replaces intent classification.

**Decision for this feature**: skip `PersonalFactsExtracted` event dispatch on the agent path. The agent can surface personal facts through a future tool or through a separate memory-extraction pass, but that's out of scope for M6. Document this as a known regression on the agent path: memory persistence does not happen automatically from conversational inputs when flag-on. Because the flag is off by default, this has no production impact in this feature's deploy.

Alternative considered and rejected: run the classify_intent call on the agent path too, just for its personal-facts extraction. Rejected because it reintroduces the Groq LLM call the agent migration is meant to eliminate.

## Error semantics — summary

| Failure | Path | Behavior |
|---------|------|----------|
| Exception before `graph.ainvoke` (e.g. taste-service 500) | agent | Caught by outer try/except → `ChatResponse(type="error", ...)` |
| Exception inside a tool | agent | Counted in `error_count`; if `>= max_errors`, routed to `fallback_node` → `type="agent"` with fallback step |
| Exception inside `agent_node` LLM call | agent | Same — counted, may route to fallback |
| Exception during `ainvoke` that LangGraph cannot catch (e.g. checkpointer write failure) | agent | Propagates out → caught by outer try/except → `type="error"` |
| `graph.ainvoke` returns without `AIMessage` | agent | Fallback node ensures an `AIMessage` is always present; this path is unreachable in practice but the `message=""` branch is the defensive default. |

## `get_agent_graph` FastAPI dependency

```python
# api/deps.py
def get_agent_graph(request: Request) -> Any:
    """Return the compiled agent StateGraph built at startup.

    Populated by `api/main.py` lifespan startup. See research.md item 4.
    """
    return request.app.state.agent_graph
```

`ChatService` is wired via the updated `get_chat_service`:

```python
async def get_chat_service(
    extraction_service: ExtractionService = Depends(get_extraction_service),
    consult_service: ConsultService = Depends(get_consult_service),
    recall_service: RecallService = Depends(get_recall_service),
    assistant_service: ChatAssistantService = Depends(get_chat_assistant_service),
    event_dispatcher: EventDispatcher = Depends(get_event_dispatcher),
    memory_service: UserMemoryService = Depends(get_user_memory_service),
    taste_service: TasteModelService = Depends(get_taste_service),
    config: AppConfig = Depends(get_config),
    agent_graph: Any = Depends(get_agent_graph),
) -> ChatService:
    return ChatService(
        extraction_service=extraction_service,
        consult_service=consult_service,
        recall_service=recall_service,
        assistant_service=assistant_service,
        intent_parser=IntentParser(),
        event_dispatcher=event_dispatcher,
        memory_service=memory_service,
        taste_service=taste_service,
        config=config,
        agent_graph=agent_graph,
    )
```

## Lifespan hook (`api/main.py`)

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    # ... existing startup (cache, event dispatcher, etc.) ...

    checkpointer = await build_checkpointer()

    recall = _build_recall_service(...)
    extraction = _build_extraction_service(...)
    consult = _build_consult_service(...)
    tools = build_tools(recall, extraction, consult)

    llm = get_llm("orchestrator")
    app.state.agent_graph = build_graph(llm, tools, checkpointer)

    yield
    # no-op teardown (research.md item 5)
```

Eager construction regardless of flag value. If `build_checkpointer` fails (Postgres unreachable), startup fails — surfacing misconfiguration at boot, not at first request.

## Bruno collection update

Add `kebi-config/bruno/chat_agent_example.bru` with a sample agent-path response. Kept as a live reference for the product-repo team whenever they begin consuming the new type. Flag-off default means no product-repo code change is blocking this feature.
