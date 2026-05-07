# Tool schemas — recall / save / consult (M5)

Internal contract for the three `@tool`-decorated async functions under `src/kebi/core/agent/tools/`. Each tool's **docstring is the LLM-facing contract** — copy verbatim from the plan doc's M5 section; field `description` text is also the LLM-facing contract.

Pattern for all three tools (LangGraph 0.3 — see research.md items 2 and 10):

```python
from typing import Annotated
from langchain_core.tools import tool, InjectedToolCallId
from langchain_core.messages import ToolMessage
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from kebi.core.agent.tools._emit import build_emit_closure, append_summary

@tool("<name>", args_schema=<Input>)
async def <tool_name>(
    # LLM-visible fields (from <Input>)
    ...,
    # Injected (hidden from LLM-visible schema)
    state: Annotated[AgentState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """<verbatim from plan doc>"""
    collected, emit = build_emit_closure("<name>")
    response = await <service>.<method>(..., emit=emit)
    append_summary(collected, "<name>", _<tool>_summary(response))
    return Command(update={
        "last_recall_results": ...,   # recall only
        "reasoning_steps": (state.get("reasoning_steps") or []) + collected,
        "messages": [ToolMessage(content=response.model_dump_json(),
                                 tool_call_id=tool_call_id)],
    })
```

## Shared helpers — `src/kebi/core/agent/tools/_emit.py`

Every wrapper uses the same fan-out pattern, factored into two helpers. This is the ONE place agent-layer field defaults (`source="tool"`, `visibility="debug"` for debug steps, `visibility="user"` for `tool.summary`), `duration_ms` computation, and stream-writer wiring live.

```python
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

    emit(step, summary, duration_ms=None):
      - Appends a debug-visibility ReasoningStep to `collected`.
      - When `duration_ms` is None, computes elapsed from timestamp
        delta (time since previous emit on this closure, or since closure
        build time for the first emit). When caller passes it explicitly,
        uses the supplied value verbatim.
      - Forwards to get_stream_writer() for live SSE when a caller is
        streaming; silent no-op otherwise.
    """
    collected: list[ReasoningStep] = []
    last_ts = datetime.now(UTC)
    writer = get_stream_writer()   # returns None when no caller is streaming

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

    `duration_ms` reflects the total tool-invocation elapsed — from the
    first emit in `collected` to now. When `collected` is empty (no debug
    emits happened before summary), `duration_ms` is 0.0.
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

Why helpers (not a plain Protocol on `EmitFn`): `EmitFn` IS a Protocol (required for the `duration_ms` default), but that only captures the call-site signature. The actual duplication across three tools is step construction + `duration_ms` computation + stream-writer wiring — that's what the helpers factor. Centralizing means Langfuse spans, metric counters, or step-field defaults change in one place and affect all three tools.

Catalog enforcement: the `ToolName` Literal means mypy flags `build_emit_closure("recal")` at the call site.

## 1. `recall_tool`

**File**: `src/kebi/core/agent/tools/recall_tool.py`
**Factory**: `build_recall_tool(service: RecallService) -> Tool`

### `RecallToolInput` (Pydantic)

| Field | Type | Default | LLM-visible description |
|-------|------|---------|-------------------------|
| `query` | `str \| None` | `None` | Verbatim from plan doc M5 (retrieval phrase or null for filter-only mode). |
| `filters` | `RecallFilters \| None` | `None` | Structural filters mirroring `PlaceObject`. |
| `sort_by` | `Literal["relevance", "created_at"]` | `"relevance"` | Ordering — relevance vs recency. |
| `limit` | `int` (`ge=1, le=50`) | `20` | Max places. |

### Tool docstring (LLM-facing — verbatim)

> Retrieve the user's saved places.
>
> Use this whenever the user wants to find, list, or recommend from their own saves. Also call this FIRST whenever the user asks for a recommendation — the result feeds into the consult tool automatically (you do not need to pass the places yourself; they are stored in agent state and picked up by consult on the next call).

### Body contract

```python
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

**Service contract**: `RecallService.run` gains an `emit: EmitFn | None = None` parameter. It calls `emit("recall.mode", f"mode={mode}; limit={limit}; sort_by={sort_by}")` immediately after the mode is determined and `emit("recall.result", f"{len(results)} places matched")` immediately after the search runs. The `limit` field on the tool input maps to `service.run(..., limit=limit, emit=emit)`.

The wrapper does NOT directly construct `recall.mode` / `recall.result` `ReasoningStep` entries — those flow through `build_emit_closure`'s returned `emit` callback which appends debug-visibility entries to `collected`. The wrapper only appends its own `tool.summary` via `append_summary`.

### `_recall_summary(query, filters, places) -> str`

Verbatim from plan doc M5:
- `query is None` → filter-mode narration: `f"Pulled your {len(places)} saved {_filter_noun(filters) or 'places'}"` or `f"No saved {what} matched those filters"`.
- `query is not None` and `not places` → `f"Checked your saves for {query} — nothing matched"`.
- `query is not None` and places → `f"Checked your saves for {query} — found {len(places)} {'match' if len(places)==1 else 'matches'}"`.

Helper `_filter_noun(filters)` derives a noun from `filters.place_type` / `filters.subcategory` / `filters.attributes.cuisine` in precedence order.

### Schema invariants (SC-008)

`RecallToolInput.model_json_schema()["properties"]` contains exactly: `query`, `filters`, `sort_by`, `limit`. Does NOT contain `user_id`, `location`, `state`, `tool_call_id`, `runtime`.

---

## 2. `save_tool`

**File**: `src/kebi/core/agent/tools/save_tool.py`
**Factory**: `build_save_tool(service: ExtractionService) -> Tool`

### `SaveToolInput` (Pydantic)

| Field | Type | Default | LLM-visible description |
|-------|------|---------|-------------------------|
| `raw_input` | `str` | *(required)* | Verbatim from plan doc M5 (pass raw URL or text). |

### Tool docstring (LLM-facing — verbatim)

> Save a place the user shared (URL or free text).
>
> Call when the user shares a URL (TikTok, Instagram, YouTube) or names a specific place they want to save. Pass the raw URL or text — do not reformat.

### Body contract

```python
collected, emit = build_emit_closure("save")
response = await service.run(raw_input, state["user_id"], emit=emit)
append_summary(collected, "save", _save_summary(response))
return Command(update={
    "reasoning_steps": (state.get("reasoning_steps") or []) + collected,
    "messages": [ToolMessage(
        content=response.model_dump_json(),
        tool_call_id=tool_call_id,
    )],
})
```

**Service contract**: `ExtractionService.run` gains an `emit: EmitFn | None = None` parameter. It emits primitive tuples at each pipeline boundary:
- `emit("save.parse_input", f"url={url}; supplementary_text={n} chars")` after input parsing.
- `emit("save.enrich", f"{n} candidates from caption + NER ({k} corroborated)")` after Phase 1.
- `emit("save.deep_enrichment", f"Phase 3 fired: {'+'.join(enrichers)}")` **only when Phase 3 enrichers (Whisper + vision) fire**. Optional heartbeat for long-running turns.
- `emit("save.validate", f"{m} validated via Google Places")` after Phase 2.
- `emit("save.persist", f"status={outcome.status}; confidence={outcome.confidence}")` after persistence.

The wrapper does NOT synthesize debug sub-steps at the tool boundary anymore — they are emitted inline by the service via `emit`. The wrapper only adds the user-visible `tool.summary`.

Save tool does NOT write `last_recall_results`.

### `_save_summary(response: ExtractPlaceResponse) -> str`

Operates on the envelope (not a single item). Matches plan-doc M5:
```python
def _save_summary(response: ExtractPlaceResponse) -> str:
    if response.status == "failed":
        return "Couldn't extract a place from that"
    if response.status == "pending":
        return "Extraction in progress — I'll update you shortly"
    # status == "completed"; at least one result
    item = response.results[0]
    name = item.place.place_name
    return {
        "saved":        f"Saved {name} to your places",
        "duplicate":    f"You already had {name} saved",
        "needs_review": f"Saved {name} — confidence is low, can you confirm?",
    }[item.status]
```

When the envelope reports multiple results (rare — current extraction pipeline typically returns one item per URL), the summary narrates the first; future work may prefer the top-confidence one.

### Schema invariants (SC-008)

`SaveToolInput.model_json_schema()["properties"]` contains exactly: `raw_input`. Does NOT contain `user_id`, `location`, `state`, `tool_call_id`.

---

## 3. `consult_tool`

**File**: `src/kebi/core/agent/tools/consult_tool.py`
**Factory**: `build_consult_tool(service: ConsultService) -> Tool`

### `ConsultToolInput` (Pydantic)

| Field | Type | Default | LLM-visible description |
|-------|------|---------|-------------------------|
| `query` | `str` | *(required)* | Verbatim from plan doc M5 (retrieval phrase describing what to recommend). |
| `filters` | `ConsultFilters` | *(required)* | Structural + discovery filters. |
| `preference_context` | `str \| None` | `None` | Verbatim from plan doc M5 (one- or two-sentence summary, relevant signals only). |

### Tool docstring (LLM-facing — verbatim)

> Recommend a place. Merges the user's saved places (from the previous recall call, available automatically via agent state) with externally discovered candidates, deduplicates, and returns ranked results.
>
> Call recall FIRST in the same turn. If the user has no saved matches, call recall anyway — consult will work with the empty list and return discoveries only.

### Body contract

```python
collected, emit = build_emit_closure("consult")
response = await service.consult(
    user_id=state["user_id"],
    query=query,
    saved_places=state.get("last_recall_results") or [],
    filters=filters,
    location=state.get("location"),
    preference_context=preference_context,
    signal_tier="active",   # agent path defaults to active; signal_tier
                            # plumbing from ChatRequest is deferred
    emit=emit,
)
append_summary(collected, "consult", _consult_summary(response))
return Command(update={
    "reasoning_steps": (state.get("reasoning_steps") or []) + collected,
    "messages": [ToolMessage(
        content=response.model_dump_json(),
        tool_call_id=tool_call_id,
    )],
})
```

**Service contract**: `ConsultService.consult(...)` gains an `emit: EmitFn | None = None` parameter and emits primitive tuples at each pipeline boundary (see `consult_service_signature.md`). The wrapper does NOT read `response.reasoning_steps` — **that field has been removed** from `ConsultResponse` per the plan-doc revision. Debug sub-steps flow through the `emit` closure into `collected` as each consult pipeline stage completes; the wrapper only adds the user-visible `tool.summary` via `append_summary`.

Consult tool does NOT write `last_recall_results` (that is recall's exclusive responsibility).

### `_consult_summary(response: ConsultResponse) -> str`

Verbatim from plan doc:
```python
def _consult_summary(response) -> str:
    saved      = sum(1 for r in response.results if r.source == "saved")
    discovered = sum(1 for r in response.results if r.source == "discovered")
    total      = saved + discovered
    if total == 0: return "Nothing matched nearby"
    if saved == 0: return f"Ranked {discovered} nearby options"
    if discovered == 0: return f"Ranked {saved} from your saves"
    return f"Ranked {total} options ({saved} saved + {discovered} nearby)"
```

### Schema invariants (SC-008 / SC-009)

`ConsultToolInput.model_json_schema()["properties"]` contains exactly: `query`, `filters`, `preference_context`. Does NOT contain `saved_places`, `user_id`, `location`, `state`, `tool_call_id`.

Additional trace-level invariant (SC-009): in captured traces of the `consult_tool` call, the LLM-visible `tool_calls[0].args` payload never contains a serialized list of PlaceObjects — that data flows via state.

---

## `build_tools(recall, extraction, consult) -> list[Tool]`

**File**: `src/kebi/core/agent/tools/__init__.py`

```python
def build_tools(
    recall: RecallService,
    extraction: ExtractionService,
    consult: ConsultService,
) -> list[Tool]:
    return [
        build_recall_tool(recall),
        build_save_tool(extraction),
        build_consult_tool(consult),
    ]
```

Tool order is stable (recall → save → consult). Not LLM-semantic (Sonnet selects by name), but stable for test assertions and debugging.

## Agent-node tool-decision step emission (M5 addition)

Feature 027's `make_agent_node` does NOT emit `agent.tool_decision` reasoning steps — it only appends the `AIMessage` and increments `steps_taken`. M5 extends `make_agent_node` to also emit one user-visible `agent.tool_decision` step per LLM call, with:

- `summary = (ai_msg.content or "").strip()[:200]` — truncated to 200 chars for JSON payload.
- Synthesized fallback when content is empty: `{"recall": "recall — user referenced saved places", "save": "save — message contains URL or named place", "consult": "consult — recommendation request"}.get(first_tool_call_name, "responding directly")`.
- `source="agent"`, `tool_name=None`, `visibility="user"`.

**Note**: this extends the 027 agent node. M5 implementation modifies `core/agent/graph.py::make_agent_node` in place; the existing 027 tests for `steps_taken` increment and `messages` append continue to pass.
