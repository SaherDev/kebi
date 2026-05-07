# Research: Chat Assistant Service

**Feature**: 016-chat-assistant-service  
**Date**: 2026-04-09

## Findings

### Decision: Model role configuration location

**Decision**: Add `chat_assistant` role to `config/app.yaml` under `models:`, not a separate file.  
**Rationale**: The constitution references `config/models.yaml` but that file doesn't exist — all model role config lives in `config/app.yaml`. Every existing role (`intent_parser`, `orchestrator`, `vision_frames`, etc.) is in `app.yaml`. Following actual repo state.  
**Alternatives considered**: Creating a separate `config/models.yaml` — rejected, would require config loader changes and diverge from existing patterns.

---

### Decision: Temperature for chat_assistant role

**Decision**: `temperature: 0.9`  
**Rationale**: Existing roles set temperature based on task type: structured extraction uses 0 (`intent_parser`, `evaluator`), creative/orchestration uses 0.7 (`orchestrator`). A conversational advisor benefits from slightly higher temperature than orchestration to produce natural, varied prose. 0.9 is within safe range for GPT-4o-mini without producing incoherent output.  
**Alternatives considered**: 0.7 (same as orchestrator) — viable but slightly conservative for conversational tone; 1.0 — standard ChatGPT default but risks occasional rambling.

---

### Decision: LLM call pattern (raw `OpenAILLMClient.complete()` vs Instructor)

**Decision**: Use `get_llm("chat_assistant")` → `LLMClientProtocol.complete()` directly.  
**Rationale**: Instructor is for structured extraction into Pydantic models. This service returns a free-text string — no schema needed. Using `OpenAILLMClient.complete()` is the correct tool and matches how `AnthropicLLMClient` is used in the orchestrator.  
**Alternatives considered**: Instructor with `str` output — unnecessary overhead; LangChain chains — no added value for a single stateless call.

---

### Decision: Error type for LLM failure → HTTP 503

**Decision**: Define `LLMUnavailableError` in `src/kebi/api/errors.py` and register a 503 handler.  
**Rationale**: ADR-023 maps: 400 bad input, 422 unparseable, 500 internal failure. HTTP 503 (Service Unavailable) is the correct status for upstream provider unavailability. Adding a named exception class keeps error handling explicit and testable, matching the existing `ValueError → 400` pattern.  
**Alternatives considered**: Raise `RuntimeError` and map to 503 — would conflict with any existing `RuntimeError → 500` mappings; handle in route handler directly — violates ADR-034 (no business logic in route handler).

---

### Decision: Langfuse tracing pattern

**Decision**: Follow the `IntentParser` pattern: `lf = get_langfuse_client()`, `generation = lf.generation(...) if lf else None`, wrap LLM call in try/except, call `generation.end()` in both success and failure branches.  
**Rationale**: Established pattern in codebase. `get_langfuse_client()` returns `None` when not configured — must guard. Generation tracking provides input/output visibility in Langfuse dashboard.  
**Alternatives considered**: Callback-based tracing (LangChain-style) — not used in this repo for non-LangGraph flows.

---

### Decision: Request validation for empty message

**Decision**: Add `min_length=1` to `message: str` field in `ChatRequest` Pydantic model.  
**Rationale**: FastAPI returns HTTP 422 automatically for Pydantic validation failures. `min_length=1` rejects empty strings and whitespace-only strings (Pydantic v2 strips before validation with `strip_whitespace=True` via `Field`). No service-layer validation needed — the boundary is enforced at schema level.  
**Alternatives considered**: Manual validation in service layer — unnecessary; Pydantic handles it cleanly at the API boundary, consistent with ADR-017.

---

### No unknowns remaining

All decisions derived from:
- Reading existing service patterns (`IntentParser`, `OpenAIVisionExtractor`, `ConsultService`)
- Reading `config/app.yaml` to understand existing model role format
- Reading `src/kebi/api/errors.py` to understand error handler registration
- Reading `src/kebi/api/deps.py` to understand dependency injection patterns
- Reading `src/kebi/providers/llm.py` to understand `get_llm()` factory
