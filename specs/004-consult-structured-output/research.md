# Research: Consult Endpoint — Structured Output (Phase 2)

## Current Codebase State

The following files already exist and are partially or fully implemented:

| File | Status | Gap |
|------|--------|-----|
| `api/schemas/consult.py` | EXISTS | Missing `photos` field in `PlaceResult`; response named `SyncConsultResponse` |
| `api/routes/consult.py` | EXISTS | Facade pattern correct; uses `orchestrator` LLM; streaming + sync modes |
| `core/consult/service.py` | EXISTS (stub) | `consult()` returns hardcoded stub; no intent parsing, no 6 steps, no photos |
| `core/intent/intent_parser.py` | MISSING | Needs to be created |
| `providers/tracing.py` | MISSING | Langfuse tracing not yet implemented (ADR-025) |
| Bruno sync consult file | MISSING | Only streaming file exists |
| `langfuse` in pyproject.toml | MISSING | Not yet a dependency |

---

## Decision: LangGraph Deferral (ADR-021 tension)

**Decision**: Defer LangGraph StateGraph to Phase 3. Phase 2 uses a sequential `ConsultService` that calls `IntentParser` then builds reasoning steps and recommendation content inline.

**Rationale**: ADR-021 mandates LangGraph for consult. However, the Phase 2 scope explicitly excludes retrieval (pgvector), discovery (Google Places), and ranking — the three steps that benefit most from parallel LangGraph branches (ADR-009). Implementing LangGraph with 6 stub nodes adds ~200 lines of scaffolding that will be fully rewritten when real nodes are added. The sequential service is not a violation of intent if Phase 3 replaces it with the graph. The route handler facade (ADR-034) and node data contracts (ADR-010) are preserved in the service layer so the LangGraph migration is non-breaking.

**Alternatives considered**:
- LangGraph with 6 stub nodes now: Rejected. All nodes would be pass-through stubs. The graph overhead (state schema, compiled graph, node wiring) is 3x more code for zero functional benefit in Phase 2.
- Skip LangGraph permanently: Rejected. ADR-021 is binding. Phase 3 will introduce the StateGraph when retrieval and discovery are added.

---

## Decision: Langfuse Tracing Implementation

**Decision**: Add `langfuse` SDK to `pyproject.toml`. Create `providers/tracing.py` with a `get_langfuse_client()` factory that returns `langfuse.Langfuse | None`. Caller wraps LLM calls with manual `langfuse.generation(...)` spans. If credentials are absent, the factory returns `None` and tracing is silently skipped with a warning.

**Rationale**: The current LLM clients (`AnthropicLLMClient`, `OpenAILLMClient`, `InstructorClient`) do not use LangChain chains, so LangChain callback handlers cannot be attached. The Langfuse Python SDK supports direct generation logging without LangChain. Using the raw SDK is simpler and avoids adding a LangChain dependency chain for tracing alone.

**Implementation pattern**:
```python
; In providers/tracing.py
def get_langfuse_client() -> langfuse.Langfuse | None:
    try:
        client = langfuse.Langfuse()
        client.auth_check()  ; raises if credentials missing
        return client
    except Exception:
        logger.warning("Langfuse not configured — tracing disabled")
        return None
```

Callers:
```python
lf = get_langfuse_client()
generation = lf.generation(name="intent_parsing", input=messages) if lf else None
result = await instructor_client.extract(...)
if generation:
    generation.end(output=result.model_dump())
```

**Alternatives considered**:
- LangChain callback handler: Rejected. Current LLM clients bypass LangChain; refactoring them to use LangChain chains for tracing alone is overengineering.
- Skip Langfuse for Phase 2: Rejected. ADR-025 is binding.

---

## Decision: `photos` Field Design

**Decision**: Add `photos: list[str]` to `PlaceResult`. Field is required and validated non-empty (`min_length=1`). For Phase 2, use a static placeholder URL. The list supports multiple photo URLs; the frontend picks the appropriate one for primary (16:9) vs alternatives (1:1).

**Rationale**: The API contract states "photos is required" with different aspect ratios for primary vs alternatives. Since both share the same `PlaceResult` schema, a `list[str]` accommodates both contexts. A required list (not optional) enforces the contract at the schema boundary.

**Phase 2 placeholder**: `https://placehold.co/800x450.webp` for all recommendations.

**Alternatives considered**:
- `photo_hero: str` + `photo_square: str` separate fields: Rejected. Primary and alternatives share `PlaceResult` — this would add nullable fields to both when only one is relevant.
- `photos: list[str] | None`: Rejected. The contract says required.

---

## Decision: Intent Parser Structure

**Decision**: New module `src/kebi/core/intent/intent_parser.py`. Defines `ParsedIntent` (Pydantic model with fields: `cuisine: str | None`, `occasion: str | None`, `price_range: str | None`, `radius: int | None`, `constraints: list[str]`). Defines `IntentParser` class using `get_instructor_client("intent_parser")` for structured extraction. `IntentParser.parse(query)` returns `ParsedIntent`. Malformed LLM output raises `ValidationError` → FastAPI returns 422.

**Rationale**: Instructor handles schema validation and retry logic transparently. The `intent_parser` role maps to `openai/gpt-4o-mini` in `app.yaml` — no config changes needed. Instructor's `extract()` already raises `ValidationError` on final schema failure, satisfying the 422 requirement.

**`InstructorClient` already exists** in `providers/llm.py` and is accessible via `get_instructor_client("intent_parser")`.

---

## Decision: `ConsultResponse` Naming

**Decision**: Rename `SyncConsultResponse` → `ConsultResponse` in `api/schemas/consult.py`. Update all references (route handler, service, tests). The `Sync` prefix is an implementation detail (vs future SSE mode), not a user-facing distinction — the schema name should match the spec and API contract.

---

## Decision: Service Architecture for Phase 2

**Decision**: `ConsultService.__init__` keeps current signature `(llm: LLMClientProtocol)`. The `orchestrator` LLM is injected via the route's `get_consult_service()`. The `IntentParser` is instantiated inside `consult()` using `get_instructor_client("intent_parser")`. This avoids changing the route handler.

**6 reasoning steps with real data**:
- `intent_parsing`: Summarizes actual parsed fields from `ParsedIntent` (e.g., "Parsed: cuisine=ramen, occasion=date night")
- `retrieval`: Phase 2 stub — "Checked saved places (retrieval deferred to Phase 3)"
- `discovery`: Phase 2 stub — "Discovery skipped (Phase 3)"
- `validation`: Phase 2 stub — "Validation skipped (Phase 3)"
- `ranking`: Phase 2 stub — counts or context from intent
- `completion`: "Found your match"

The `intent_parsing` step carries real data. The other 5 are honest Phase 2 stubs that don't claim false counts.
