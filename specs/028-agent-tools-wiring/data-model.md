# Phase 1 — Data Model (028-agent-tools-wiring)

Entity shapes, field types, constraints, and transitions. Every Pydantic model is declared at a module boundary or on the tool input surface — constitution Principle IV is satisfied throughout.

---

## 0. `EmitFn` — primitive callback type (Protocol)

**Location**: `src/kebi/core/emit.py` (NEW — small module).
**Kind**: `typing.Protocol` (required; not a plain `Callable` alias, because the third positional argument has a default).
**Purpose**: Cross-cutting emission pattern introduced in the plan-doc revision. Services (`RecallService`, `ConsultService`, `ExtractionService`) accept an optional `emit: EmitFn | None` parameter and call `emit(step_name, summary)` — or `emit(step_name, summary, duration_ms=elapsed)` when they measured the operation — at each pipeline boundary with primitive values. Tool wrappers supply a closure that adds agent-layer fields (`source`, `tool_name`, `visibility`, `timestamp`, `duration_ms`) and fans out to `langgraph.config.get_stream_writer()`.

```python
# src/kebi/core/emit.py
from typing import Protocol


class EmitFn(Protocol):
    def __call__(
        self,
        step: str,
        summary: str,
        duration_ms: float | None = None,
    ) -> None: ...
```

Contract invariants:
- Services never construct `ReasoningStep` objects. They call `emit` with primitives.
- Services never import from `core/agent/*` — the `EmitFn` Protocol lives in `core/emit.py` so services can import it without pulling in the agent layer.
- Structurally typed: any callable matching the signature satisfies it (spy callables in tests, production closures in wrappers, no-op `lambda _s, _m, _d=None: None` as the default `or`-coalesce fallback inside services).
- Two valid call forms at every emit site:
  - `emit("consult.discover", "5 candidates from Google")` — closure computes `duration_ms` from timestamp delta to the previous emit (or to closure build time for the first emit).
  - `emit("consult.discover", "5 candidates from Google", duration_ms=412.5)` — service measured the Google call directly and passes its own timer reading.

---

## 0a. `ReasoningStep` — `duration_ms` field addition

**Location**: `src/kebi/core/agent/reasoning.py` (EDIT — shipped in feature 027; one field added in this feature).

Add a new field:
```python
class ReasoningStep(BaseModel):
    step: str
    summary: str
    source: Literal["tool", "agent", "fallback"]
    tool_name: Literal["recall", "save", "consult"] | None = None
    visibility: Literal["user", "debug"] = "user"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    duration_ms: float | None = None                # NEW — elapsed time for this step

    @model_validator(mode="after")
    def _source_tool_name_consistency(self) -> ReasoningStep: ...   # UNCHANGED from 027
```

Contract invariants:
- `duration_ms` is `float | None` on the Pydantic model, but the wrapper's emit closure and `append_summary` helper always populate it to a non-null value before the step lands in `collected`. A `None` value surviving to the final state trace is a bug (tested in `test_emit_helpers.py`).
- On the agent path, `duration_ms` is serialized in `ChatResponse.data.reasoning_steps` via `ReasoningStep.model_dump(mode="json")` — downstream consumers can read per-step elapsed time for perf observability without hitting Langfuse.
- The existing `_source_tool_name_consistency` validator (feature 027) is unchanged. No new validator for `duration_ms` — the field is structurally independent of source/tool_name.
- Aligns with structured-logging standards (step duration is the primary debugging signal for evals and perf regressions).

---

## 1. `PlaceFilters` — shared filter base

**Location**: `src/kebi/core/places/filters.py` (NEW).
**Kind**: Pydantic `BaseModel`.
**Purpose**: Common filter shape for any tool or service that operates on places. Mirrors `PlaceObject` 1:1 per ADR-056.

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `place_type` | `PlaceType \| None` | `None` | Imports `PlaceType` enum from `core/places/models.py`. |
| `subcategory` | `str \| None` | `None` | Free-form subcategory tag. |
| `tags_include` | `list[str] \| None` | `None` | Inclusion filter; None = no tag filter. Empty list = require zero tags (rare). |
| `attributes` | `PlaceAttributes \| None` | `None` | Imports `PlaceAttributes` from `core/places/models.py`. Carries `cuisine`, `price_hint`, `ambiance`, `dietary`, `good_for`, `location_context`. |
| `source` | `PlaceSource \| None` | `None` | Imports `PlaceSource` enum. |

No validators; Pydantic type checking is sufficient. No state transitions.

---

## 2. `RecallFilters` — retrieval-time constraints

**Location**: `src/kebi/core/recall/types.py` (REWRITE — currently dataclass; migrate to Pydantic extending `PlaceFilters`).
**Kind**: Pydantic `BaseModel` extending `PlaceFilters`.
**Purpose**: Tool input shape for `recall_tool`; also used by the flag-off `ChatService._dispatch` recall branch and by `RecallService.run`.

Fields inherited from `PlaceFilters`, plus:

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `max_distance_km` | `float \| None` | `None` | Maximum distance from user's location; None = no distance filter. |
| `created_after` | `datetime \| None` | `None` | Only places saved after this timestamp. |
| `created_before` | `datetime \| None` | `None` | Only places saved before this timestamp. |

**Migration impact**:
- `@dataclass` decorator removed; class body + field declarations stay nearly identical (Pydantic and dataclass share keyword-argument construction).
- `field()` import no longer needed.
- `core/recall/service.py`, `db/repositories/recall_repository.py`, `core/chat/service.py::_filters_from_parsed`, and every test fixture that constructs `RecallFilters(...)` continues to work without modification (kwargs-based construction is identical).

**Invariant**: None enforced at model level. Call sites must validate that `created_after <= created_before` when both are set — not a spec requirement; tracked as a tech-debt note for a later tightening pass.

---

## 3. `ConsultFilters` — discovery-time constraints

**Location**: `src/kebi/core/places/filters.py` (NEW, sibling of `PlaceFilters`).
**Kind**: Pydantic `BaseModel` extending `PlaceFilters`.
**Purpose**: Tool input shape for `consult_tool`; passed to `ConsultService.consult(...)`.

Fields inherited from `PlaceFilters`, plus:

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `radius_m` | `int \| None` | `None` | Google Places discovery radius. `None` means default-from-config (currently 1500m per `consult.default_radius_m`). |
| `search_location_name` | `str \| None` | `None` | Named location ("Shibuya", "SoHo") to geocode before discovery. Mutually-exclusive-in-intent with passing a raw `Location` — the service resolves precedence. |
| `discovery_filters` | `dict[str, Any] \| None` | `None` | Passthrough for Google Places query params (`opennow`, `minprice`, `maxprice`, etc.). Opaque to consult's logic. |

No validators. Defaults are Pydantic `None`, resolved to config values inside `ConsultService.consult` (same as today).

---

## 4. `RecallToolInput` — recall tool's LLM-visible schema

**Location**: `src/kebi/core/agent/tools/recall_tool.py` (NEW).
**Kind**: Pydantic `BaseModel`.
**Purpose**: `@tool("recall", args_schema=RecallToolInput)` — the schema Sonnet sees when deciding how to call the recall tool.

| Field | Type | Default | `Field(description=...)` text (verbatim from plan doc) |
|-------|------|---------|---------------------------------------------------------|
| `query` | `str \| None` | `None` | "Retrieval phrase, rewritten from the user's message into a short noun phrase describing the place type or topic. Examples: 'find me a good ramen spot nearby' -> query='ramen restaurant'; 'that museum in Bangkok' -> query='museum Bangkok'; 'the hotel I liked in Tokyo' -> query='hotel Tokyo'; 'saved places in Japan' -> query='Japan'. Pass null for meta-queries like 'show me all my saves' or 'places from TikTok' — that triggers filter-only mode and uses no embedding search." |
| `filters` | `RecallFilters \| None` | `None` | "Structural filters on the user's saves. Mirror PlaceObject — place_type, subcategory, tags_include, nested attributes (cuisine, price_hint, ambiance, etc.)." |
| `sort_by` | `Literal["relevance", "created_at"]` | `"relevance"` | "Ordering. relevance = hybrid search score; created_at = most recently saved first (use for meta-queries)." |
| `limit` | `int` (`Field(ge=1, le=50)`) | `20` | "Max places to return. Default 20." |

**Schema invariant (SC-008)**: `RecallToolInput.model_json_schema()["properties"]` must contain exactly these four keys. `user_id` and `location` must NOT appear.

---

## 5. `SaveToolInput` — save tool's LLM-visible schema

**Location**: `src/kebi/core/agent/tools/save_tool.py` (NEW).
**Kind**: Pydantic `BaseModel`.

| Field | Type | Default | `Field(description=...)` text (verbatim from plan doc) |
|-------|------|---------|---------------------------------------------------------|
| `raw_input` | `str` | *(required)* | "Call when the user shares a URL (TikTok, Instagram, YouTube) or names a specific place they want to save. Pass the raw URL or text — do not reformat." |

**Schema invariant (SC-008)**: one key only. `user_id` / `location` absent.

---

## 6. `ConsultToolInput` — consult tool's LLM-visible schema

**Location**: `src/kebi/core/agent/tools/consult_tool.py` (NEW).
**Kind**: Pydantic `BaseModel`.

| Field | Type | Default | `Field(description=...)` text (verbatim from plan doc) |
|-------|------|---------|---------------------------------------------------------|
| `query` | `str` | *(required)* | "Retrieval phrase describing what to recommend, rewritten from the user's message. Examples: 'where should I eat tonight?' -> query='dinner restaurant'; 'I need a quiet place to work' -> query='quiet cafe laptop work'; 'something to do on a rainy afternoon' -> query='indoor activity'; 'a hotel near Shibuya' -> query='hotel Shibuya'." |
| `filters` | `ConsultFilters` | *(required)* | "Structural + discovery filters. Mirror PlaceObject plus radius_m, search_location_name, and discovery_filters for Google Places passthrough." |
| `preference_context` | `str \| None` | `None` | "One- or two-sentence summary composed from taste_profile_summary and memory_summary, limited to signals RELEVANT to this request. Example for a dinner request: 'Prefers casual spots over formal. Wheelchair user. Avoids pork.' Example for a museum request: 'Likes contemporary art. Visits on weekdays. Wheelchair user.' Omit irrelevant signals." |

**Schema invariant (SC-008/SC-009)**: three keys only. `saved_places`, `user_id`, `location` must NOT appear. Saved places flow via `state["last_recall_results"]`.

---

## 7. `ConsultService.consult(...)` — new signature

**Location**: `src/kebi/core/consult/service.py` (MAJOR EDIT).

```python
async def consult(
    self,
    user_id: str,
    query: str,
    saved_places: list[PlaceObject],
    filters: ConsultFilters,
    location: Location | None = None,
    preference_context: str | None = None,
    signal_tier: str = "active",
    emit: EmitFn | None = None,
) -> ConsultResponse:
    ...
```

**Emit parameter contract** (plan-doc revision, research.md item 10):
- Optional. When unset, defaults to a no-op via `_emit = emit or (lambda _s, _m: None)` at the top of the body.
- Called at each pipeline boundary with primitive `(step_name, summary)` strings matching the M5 catalog: `consult.geocode` (when a location name resolves), `consult.discover`, `consult.merge`, `consult.dedupe`, `consult.enrich`, `consult.tier_blend` (warming tier only), `consult.chip_filter` (active tier only, when rejected chips filter candidates).
- Services never build `ReasoningStep` instances. The wrapper's closure stamps agent-layer fields and fans out to the stream writer.

**Constructor change**: drop `intent_parser: IntentParser` and `memory_service: UserMemoryService` from `__init__`. Keep `recall_service`, `places_client`, `places_service`, `taste_service`, `recommendation_repo`. The recall service dependency is retained at the constructor level because the flag-off `ChatService._dispatch` consult branch needs a reference to it (new injection path via `api/deps.py`) — however, `ConsultService.consult` no longer calls recall itself; `saved_places` is passed in by the caller.

Actually: since the caller pre-loads saved places and passes them in, `ConsultService` has no reason to hold a `recall_service` reference either. Drop it too. The flag-off chat dispatch calls `RecallService` directly (its own dep); consult gets the results.

**Final constructor signature**:
```python
def __init__(
    self,
    places_client: PlacesClient,
    places_service: PlacesService,
    taste_service: TasteModelService,
    recommendation_repo: RecommendationRepository | None = None,
) -> None: ...
```

**Body changes**:
- Remove the `await self._memory.load_memories(user_id)` call (lines ~94–96).
- Remove the main-path `await self._taste_service.get_taste_profile(user_id)` call (lines ~98–111). The chip-filter branch retains its taste-service read under `if signal_tier == "active"` (ADR-061).
- Remove `await self._intent_parser.parse(query, user_memories=..., taste_summary=...)` (lines ~118–122). Replace with direct use of `filters` and `query`.
- Remove the internal `await self._recall_service.run(search_query, user_id)` call (line ~145). Replace with the caller-supplied `saved_places` argument.
- **Delete the internal `reasoning_steps: list[ReasoningStep] = []` list and every `reasoning_steps.append(_consult_step(...))` call.** Replace each append site with one `_emit(step_name, summary)` call using the catalog step names (`consult.geocode`, `consult.discover`, `consult.merge`, `consult.dedupe`, `consult.enrich`, `consult.tier_blend`, `consult.chip_filter`). The `_consult_step` helper landed in feature 027 becomes dead code and is removed.
- Geocoding branch: `if filters.search_location_name: filters.search_location = await self._places_client.geocode(...)` (where `search_location` is now a computed-at-runtime derived value; see `core/consult/service.py` refactor notes — might land as a private helper or as mutation of the filters object).
- Radius default: `radius_m = filters.radius_m or config.consult.default_radius_m`.
- Discovery keyword: `discovery_filters["keyword"] = query` (raw query; no `enriched_query` fallback since intent parser is gone).
- Warming-tier blend: unchanged semantically; the `warming_blend` step-name append is replaced by `_emit("consult.tier_blend", f"discovered={...}, saved={...}")`.
- Active-tier rejected-chip filter: unchanged semantically; the `active_rejected_filter` / `active_confirmed_signals` appends become `_emit("consult.chip_filter", ...)` calls.
- Persistence via `_persist_recommendation`: simplified. Drop the `reasoning_steps` argument; drop the `reasoning_steps=...` field from the `ConsultResponse(...)` used to build the JSONB payload. Historical recommendations may still contain the key in their JSONB `response` column — Postgres doesn't care (extra keys are ignored on read).

**Error**: `NoMatchesError(query)` preserved.

**Invariant**: `saved_places` parameter is required (no default). Calling `consult()` without passing it is a TypeError caught by mypy — satisfying spec clarification Q2 ("no fallback inside the consult service itself").

---

## 7a. `RecallService.run(...)` — `emit` parameter addition

**Location**: `src/kebi/core/recall/service.py` (LIGHT EDIT).

Signature gains an optional `emit: EmitFn | None = None` parameter. The body inserts `_emit = emit or (lambda _s, _m: None)` near the top and calls:
- `_emit("recall.mode", f"mode={mode}; limit={limit}; sort_by={sort_by}")` immediately after the retrieval mode is determined.
- `_emit("recall.result", f"{len(results)} places matched")` immediately after the database search returns (hybrid or filter-only).

`RecallResponse` envelope is unchanged — no `reasoning_steps` field added to the response; emission is pure side-effect via the callback.

## 7b. `ExtractionService.run(...)` — `emit` parameter addition

**Location**: `src/kebi/core/extraction/service.py` (LIGHT EDIT).

Signature gains an optional `emit: EmitFn | None = None` parameter. The inline-await body (from feature 027 M1) inserts `_emit = emit or (lambda _s, _m: None)` and calls:
- `_emit("save.parse_input", f"url={url}; supplementary_text={n} chars")` after input parsing.
- `_emit("save.enrich", f"{n} candidates from caption + NER ({k} corroborated)")` after Phase 1 enrichment.
- `_emit("save.deep_enrichment", f"Phase 3 fired: {'+'.join(enrichers)}")` **only when Phase 3 enrichers (Whisper transcript + vision caption) fire**. This is the optional heartbeat signal for long-running turns; no special mechanism, just an additional emit site inside the Phase 3 branch.
- `_emit("save.validate", f"{m} validated via Google Places")` after Phase 2 validation.
- `_emit("save.persist", f"status={outcome.status}; confidence={outcome.confidence}")` after persistence.

`ExtractPlaceResponse` envelope shape is unchanged from feature 027's M0.5 shape — no `metadata` field added, no `reasoning_steps` field added.

## 7c. `ConsultResponse` — drop `reasoning_steps` field

**Location**: `src/kebi/api/schemas/consult.py` (EDIT).

Before (feature 027):
```python
class ConsultResponse(BaseModel):
    recommendation_id: str | None = None
    results: list[ConsultResult]
    reasoning_steps: list[ReasoningStep]   # <-- REMOVED
```

After:
```python
class ConsultResponse(BaseModel):
    recommendation_id: str | None = None
    results: list[ConsultResult]
```

Per the plan-doc revision — steps are now delivered live via the `emit` callback; the response no longer bundles them.

**Downstream impact audit**:
- `core/consult/service.py::_persist_recommendation` — drop the `reasoning_steps` parameter; drop the `reasoning_steps=...` kwarg from the `ConsultResponse(...)` it constructs for JSONB storage.
- `api/schemas/consult.py` — the `ReasoningStep` re-export landed in 027 remains in place (other importers use it); only the `ConsultResponse.reasoning_steps` field is removed.
- `core/chat/service.py` flag-off consult branch — does NOT read `consult_result.reasoning_steps`; no change required.
- `tests/core/consult/test_service.py` — rewrite four existing tests that assert on `response.reasoning_steps` (warming-blend surfaced, active-rejected-chip filtered, active-confirmed-chips surfaced, warming-blend skipped in non-warming tier) to spy on `emit` instead. Already listed in FR-035(h).
- **Historical JSONB** — existing `Recommendation.response` rows contain the `reasoning_steps` key. Postgres doesn't care. No migration required; Pydantic tolerates extra keys at deserialization time (default behavior).

## 8. `ChatResponse` — Literal tightening + new value

**Location**: `src/kebi/api/schemas/chat.py` (EDIT).

```python
ChatResponseType = Literal[
    "extract-place",
    "consult",
    "recall",
    "assistant",
    "clarification",
    "error",
    "agent",          # NEW (M6)
]

class ChatResponse(BaseModel):
    type: ChatResponseType
    message: str
    data: dict[str, Any] | None = None
```

Docstring updated to list all seven values. `data` continues to be `dict[str, Any]`; on the agent path it carries `{"reasoning_steps": [<ReasoningStep.model_dump()>, ...]}`.

**Invariant**: `type="agent"` responses MUST populate `data.reasoning_steps` with a non-null list (possibly empty). Flag-off paths never emit `type="agent"`.

---

## 9. Compiled agent graph (FastAPI app-state attribute)

**Location**: `src/kebi/api/main.py` (lifespan) + `src/kebi/api/deps.py` (`get_agent_graph`).

```python
# api/main.py (inside lifespan async context manager)
checkpointer = await build_checkpointer()
tools = build_tools(recall, extraction, consult)
llm = get_llm("orchestrator")
app.state.agent_graph = build_graph(llm, tools, checkpointer)
yield
# teardown: no-op (see research.md item 5)
```

```python
# api/deps.py
from fastapi import Request

def get_agent_graph(request: Request) -> Any:
    """Return the compiled agent StateGraph built at startup."""
    return request.app.state.agent_graph
```

**Kind**: Not a Pydantic model. Application-level singleton stored on `app.state`. Its type is LangGraph's `CompiledStateGraph[AgentState]`.

**Lifecycle**:
- Built ONCE per process, before the first request.
- Read-only after construction — no mutations during request handling.
- Shared across all requests; LangGraph internals are concurrency-safe for parallel `ainvoke` calls on the same compiled graph.

---

## 10. `ChatService._run_agent` — invocation shape

**Location**: `src/kebi/core/chat/service.py` (EDIT — new private method).

Input: `ChatRequest`.

Sequence:
1. `taste_profile_summary: str = await format_taste_summary(user_id)` — single-call helper; loads taste profile via `TasteModelService.get_taste_profile`, formats via `format_summary_for_agent`. (Helper already exists in `core/taste/regen.py`.)
2. `memory_summary: str = await format_memory_summary(user_id)` — loads via `UserMemoryService.load_memories`, joins with newlines.
3. `payload = build_turn_payload(message=request.message, user_id=request.user_id, taste_profile_summary=..., memory_summary=..., location=request.location.model_dump() if request.location else None)` — resets transient fields (from feature 027).
4. `config = {"configurable": {"thread_id": request.user_id}, "metadata": {"user_id": request.user_id, "session_id": uuid4().hex}}`.
5. `result = await self._graph.ainvoke(payload, config=config)` — result is the final `AgentState` after graph termination.
6. `ai_message = next((m for m in reversed(result["messages"]) if isinstance(m, AIMessage)), None)` — last AIMessage from the graph.
7. `user_steps = [s for s in result["reasoning_steps"] if s.visibility == "user"]` — filter for JSON payload.
8. Return `ChatResponse(type="agent", message=ai_message.content if ai_message else "", data={"reasoning_steps": [s.model_dump() for s in user_steps]})`.

**Error path**: if `graph.ainvoke` raises, fall through to `ChatService.run`'s existing outer `try/except` which returns `ChatResponse(type="error", message="Something went wrong, please try again.", data={"detail": str(exc)})`. The fallback node catches errors inside the graph; the outer try/except catches errors before or during invocation (missing graph, checkpointer down, etc.).

**Constructor change**: `ChatService.__init__` gains an `agent_graph: Any` parameter and an `agent_enabled_reader: Callable[[], bool]` OR receives the AppConfig reference. Simpler: pass `config: AppConfig`; read `self._config.agent.enabled` per request.

---

## 11. `ChatService._dispatch` — consult branch (flag-off scaffolding per spec Q2)

**Location**: `src/kebi/core/chat/service.py` (EDIT).

Today the consult branch reads `request.message` and calls `self._consult.consult(user_id, message, location)`. After this feature:

```python
if intent == "consult":
    # Flag-off scaffolding: build ConsultFilters + load saved places ourselves,
    # then call the refactored ConsultService.consult(...).
    saved_places = await self._load_saved_places_for_consult(
        user_id=request.user_id,
        message=request.message,
    )
    filters = ConsultFilters()  # empty filter set — legacy path doesn't parse
                                # intent into structured filters; this is a known
                                # quality regression relative to the old
                                # intent-parsed path. Acceptable because:
                                # (a) flag-off is for continuity only during
                                #     migration; quality lives on the agent path.
                                # (b) Quality canary in M10 gates the flag flip.
    try:
        consult_result = await self._consult.consult(
            user_id=request.user_id,
            query=request.message,
            saved_places=saved_places,
            filters=filters,
            location=request.location,
            preference_context=None,
            signal_tier="active",  # default; signal_tier carryover optional
        )
    except NoMatchesError:
        return ChatResponse(type="assistant", ...)
    ...
```

Where `_load_saved_places_for_consult` is a new private helper that calls `self._recall.run(query=request.message, user_id=request.user_id, filters=None)` and returns `[r.place for r in response.results]`.

**Note**: this IS a known quality regression on the flag-off path (no intent-parsed filters, no enriched query). Documented here and acknowledged in the spec's Edge Cases. The legacy path is explicitly temporary — deleted in the next feature (M11). The agent path (flag-on) does not regress because it replaces intent-parsing with Sonnet's per-turn decisions.

---

## 12. Reasoning-step catalog (reference, not new types)

Per the plan doc's M5 catalog — enforced by `ReasoningStep.model_validator` (already in 027) + per-tool emission discipline in M5:

**User-visible (exactly three types — SC-004)**:
| `step` | `source` | `tool_name` | Emitter |
|--------|----------|-------------|---------|
| `agent.tool_decision` | `"agent"` | `None` (always) | `agent_node` — one per LLM call |
| `tool.summary` | `"tool"` | `"recall"` / `"save"` / `"consult"` | Each tool wrapper — one per invocation |
| `fallback` | `"fallback"` | `None` | `fallback_node` (from 027) |

**Debug-only** (surface in Langfuse and — once SSE ships in M7 — in debug stream):
| `step` | `source` | `tool_name` | Emitter |
|--------|----------|-------------|---------|
| `recall.mode` | `"tool"` | `"recall"` | `recall_tool` |
| `recall.result` | `"tool"` | `"recall"` | `recall_tool` |
| `save.parse_input` | `"tool"` | `"save"` | `save_tool` |
| `save.enrich` | `"tool"` | `"save"` | `save_tool` |
| `save.validate` | `"tool"` | `"save"` | `save_tool` |
| `save.persist` | `"tool"` | `"save"` | `save_tool` |
| `consult.discover` | `"tool"` | `"consult"` | `consult_tool` |
| `consult.merge` | `"tool"` | `"consult"` | `consult_tool` |
| `consult.dedupe` | `"tool"` | `"consult"` | `consult_tool` |
| `consult.enrich` | `"tool"` | `"consult"` | `consult_tool` |
| `consult.tier_blend` | `"tool"` | `"consult"` | `consult_tool` (warming tier only) |
| `consult.chip_filter` | `"tool"` | `"consult"` | `consult_tool` (active tier only) |
| `consult.geocode` | `"tool"` | `"consult"` | `consult_tool` (when location-name resolved) |

Consult-tool's debug steps forward the existing `ConsultResponse.reasoning_steps` (from 027 helper `_consult_step`) which already sets `source="tool", tool_name="consult", visibility="debug"` — no change required on the consult service side.

---

## State transitions — unchanged

Per-turn state flow is identical to feature 027:

1. **Turn boundary**: `build_turn_payload` resets `last_recall_results=None`, `reasoning_steps=[]`, `steps_taken=0`, `error_count=0`. `messages` appends via `add_messages` reducer (carries prior turns' history).
2. **Agent node**: reads system prompt template, appends `AIMessage`, increments `steps_taken`. Emits one user-visible `agent.tool_decision` step (M5 addition — the existing 027 agent node does NOT emit this step; M5 adds it).
3. **Tool node**: LangGraph's `ToolNode` invokes the selected tool; the tool's `Command(update=...)` writes `last_recall_results` (recall only), extends `reasoning_steps`, and appends a `ToolMessage`.
4. **Continue check** (`should_continue`): `error_count >= max_errors` or `steps_taken >= max_steps` → fallback; last message has `tool_calls` → tools; else → END.
5. **Fallback node**: emits one user-visible `fallback` step (from 027) + terminal `AIMessage`.
6. **Checkpointer**: persists the final `AgentState` under `thread_id=user_id` after every turn.
