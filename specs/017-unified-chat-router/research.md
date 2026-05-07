# Research: Unified Chat Router

**Branch**: `017-unified-chat-router` | **Date**: 2026-04-09

## Existing Service Interfaces (verified from source)

### ConsultService
- File: `src/kebi/core/consult/service.py`
- Method: `async def consult(user_id: str, query: str, location: Location | None) -> ConsultResponse`
- **Already non-streaming.** The existing `/v1/consult` route calls `service.consult()` and returns `JSONResponse(result.model_dump())`. No streaming adapter is needed — clarification Q1 (Option B) is already satisfied by the current code.
- Dep injection: `get_consult_service()` in `api/deps.py` (line 266)

### ExtractionService
- File: `src/kebi/core/extraction/service.py`
- Method: `async def run(raw_input: str, user_id: str) -> ExtractPlaceResponse`
- Returns a Pydantic model.

### RecallService
- File: `src/kebi/core/recall/service.py`
- Method: `async def run(query: str, user_id: str) -> RecallResponse`
- Returns a Pydantic model.

### ChatAssistantService
- File: `src/kebi/core/chat/chat_assistant_service.py`
- Method: `async def run(message: str, user_id: str) -> str`
- Returns a **plain string**, not a Pydantic model. ChatService must wrap it: `ChatResponse(type="assistant", message=result, data=None)`.

---

## Schema Package Layout

Schemas live in `src/kebi/api/schemas/` (a package, not a single file). The spec says "add to `api/schemas.py`" — this is incorrect. New chat schemas belong in:
- `src/kebi/api/schemas/chat.py` — new file for `ChatRequest` and `ChatResponse`

The existing `Location` Pydantic model lives in `api/schemas/consult.py`. `ChatRequest.location` should reuse this type (`Location | None`) rather than using `dict | None` as the spec suggests — `dict` at a Pydantic boundary violates Constitution Section IV.

---

## Config Pattern (app.yaml)

- Decision: `intent_router` config entry belongs in `config/app.yaml` under `models:`, not in `config/.local.yaml`. Model chosen: **Groq Llama 3.1 8B** (`llama-3.1-8b-instant`).
- Rationale: `.local.yaml` is for secrets (ADR-051). Model role config is non-secret, correct target is `app.yaml`. Groq is already in the stack (`groq` provider + `GROQ_API_KEY` in secrets). Llama 3.1 8B via Groq targets ~100ms for intent classification — meaningfully faster than GPT-4o-mini (~400–600ms) for a 4-way classification task that doesn't require reasoning depth. A JSON parse fallback handles the rare malformed response.
- Format to add:
  ```yaml
  intent_router:
    provider: groq
    model: llama-3.1-8b-instant
    max_tokens: 256
    temperature: 0
  ```
- Alternatives considered: GPT-4o-mini (OpenAI, ~400–600ms, more reliable JSON) — rejected because latency gain with Groq is user-perceptible in a chat UI and the classification task is well within Llama 3.1 8B's capability.

---

## Bruno Collection Location

- Actual path: `kebi-config/bruno/ai-service/` (not `kebi-config/bruno/`)
- Files to delete: `chat-assistant.bru`, `consult.bru`, `extract-place.bru`, `extract-place-status.bru`, `recall.bru`
- File to add: `chat.bru` with 5 request bodies (one per intent + one "fuji" clarification)
- Files to keep: `feedback-accepted.bru`, `feedback-rejected.bru`, `health.bru`

---

## Constitution Violations — Required ADRs

### Violation 1: Table name `recommendations` conflicts with NestJS ownership
- Constitution Section VI states "NestJS writes: users, user_settings, recommendations".
- The spec adds a `recommendations` table managed by Alembic in this repo.
- **Resolution**: Rename the table to `consult_logs`. This repo tracks AI recommendation history under a name it owns, distinct from the product table NestJS manages.
- **New ADR required** (ADR-052): "AI repo adds `consult_logs` table — AI-generated recommendation history, distinct from the product `recommendations` table owned by NestJS."

### Violation 2: Deleting routes/extract_place.py and routes/consult.py conflicts with ADR-018
- ADR-018: "Separate router modules: routes/extract_place.py and routes/consult.py"
- The feature consolidates all routes into routes/chat.py and deletes the individual modules.
- **Resolution**: This is a natural evolution. ADR-018 is superseded.
- **New ADR required** (ADR-053): "Consolidate all endpoints into routes/chat.py, superseding ADR-018."

### Violation 3: API contract changes require docs/api-contract.md update
- Constitution Section VIII documents three endpoints. The feature replaces them with one.
- **Resolution**: `docs/api-contract.md` must be updated as part of implementation. Not a blocking gate — it's a documentation task within scope.

---

## Dependency Injection for ChatService

`ChatService` needs all four downstream services plus a DB session (to write `consult_logs`). Pattern from existing deps:
- `get_chat_service()` in `api/deps.py` accepts all four services via `Depends()` and a DB session
- `ConsultLogRepository` follows the existing `SQLAlchemy*Repository` pattern in `db/repositories/`
- `ChatService.__init__` receives injected dependencies, never constructs them internally (ADR-019)

---

## Route Consolidation — Feedback Route Preserved

The spec lists four routes to delete. The `feedback` route (`routes/feedback.py`) is **not** in scope for deletion — it remains registered in `main.py`.
