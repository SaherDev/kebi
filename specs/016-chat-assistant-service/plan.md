# Implementation Plan: Chat Assistant Service

**Branch**: `016-chat-assistant-service` | **Date**: 2026-04-09 | **Spec**: [spec.md](spec.md)  
**Input**: Feature specification from `/specs/016-chat-assistant-service/spec.md`

## Summary

Build `ChatAssistantService` — a stateless, single-turn food and dining advisor. The service takes a message and user_id, calls GPT-4o-mini via the provider abstraction layer (`get_llm("chat_assistant")`), and returns a conversational string response. The system prompt positions the LLM as a knowledgeable, opinionated food advisor covering destination food scenes, food culture knowledge, dining etiquette, and discovery strategies. No RAG, no pgvector, no ranking. Langfuse traces every call. LLM failures surface as HTTP 503.

## Technical Context

**Language/Version**: Python 3.11  
**Primary Dependencies**: FastAPI 0.115, Pydantic 2.10, OpenAI SDK, Langfuse  
**Storage**: None (stateless — no DB, no Redis)  
**Testing**: pytest with `asyncio_mode = "auto"`, mocked LLM via `unittest.mock.AsyncMock`  
**Target Platform**: Linux server (Railway)  
**Project Type**: web-service  
**Performance Goals**: Response in under 10 seconds (spec SC-001)  
**Constraints**: No hardcoded model names; all via `config/app.yaml`; mypy strict must pass  
**Scale/Scope**: Same scale as existing endpoints; no new infrastructure

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| ADR | Requirement | Status |
|-----|------------|--------|
| ADR-001 | src layout `src/kebi/` | ✓ All new files under `src/kebi/` |
| ADR-002 | Hybrid directory: `api/`, `core/`, `providers/`, `eval/` | ✓ New domain `core/chat/` follows hybrid pattern |
| ADR-003 | Ruff + mypy strict | ✓ Verified in Done criteria |
| ADR-004 | Tests mirror src structure | ✓ `tests/core/chat/` mirrors `src/kebi/core/chat/` |
| ADR-014 | `/v1` prefix via APIRouter, loaded from `app.yaml` | ✓ Route registered on existing `router` with prefix from config |
| ADR-016 | `config/app.yaml` maps logical roles → provider + model | ✓ `chat_assistant` role added to `app.yaml` |
| ADR-017 | Pydantic BaseModel for all request/response schemas | ✓ `ChatRequest`, `ChatResponse` are Pydantic models |
| ADR-018 | Separate router module per endpoint | ✓ `routes/chat_assistant.py` is a new file |
| ADR-019 | FastAPI `Depends()` for all service construction | ✓ `get_chat_assistant_service()` dep in `deps.py` |
| ADR-020 | Provider abstraction — `get_llm("chat_assistant")`, never hardcoded | ✓ Role string only; model name lives in `app.yaml` |
| ADR-023 | HTTP error mapping: 400 bad input, 422 unparseable, 503 upstream unavailable | ✓ `LLMUnavailableError` registered with 503 handler |
| ADR-025 | Langfuse handler on every LLM call | ✓ `get_langfuse_client()` used in service, generation tracked |
| ADR-034 | Route handler = one service call, no business logic | ✓ Handler delegates entirely to `ChatAssistantService.run()` |
| ADR-044 | Prompt injection mitigation (XML tags + defensive system prompt) | N/A — no retrieved content injected into prompts |

**Result: PASS. No violations.**

## Project Structure

### Documentation (this feature)

```text
specs/016-chat-assistant-service/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   └── chat-assistant.md
└── tasks.md             # Phase 2 output (/speckit.tasks command)
```

### Source Code (repository root)

```text
config/
└── app.yaml             # Add chat_assistant model role

src/kebi/
├── api/
│   ├── deps.py          # Add get_chat_assistant_service()
│   ├── errors.py        # Add LLMUnavailableError + 503 handler
│   ├── main.py          # Register chat_assistant_router
│   ├── routes/
│   │   └── chat_assistant.py   # NEW: POST /chat-assistant handler
│   └── schemas/
│       └── chat_assistant.py   # NEW: ChatRequest, ChatResponse
└── core/
    └── chat/
        ├── __init__.py                 # NEW
        └── chat_assistant_service.py   # NEW: ChatAssistantService

tests/
└── core/
    └── chat/
        ├── __init__.py                         # NEW
        └── test_chat_assistant_service.py      # NEW: unit tests
```

## Implementation Tasks

### Task 1: Add `chat_assistant` model role to `config/app.yaml`

Add under `models:`:

```yaml
chat_assistant:
  provider: openai
  model: gpt-4o-mini
  max_tokens: 1024
  temperature: 0.9
```

Temperature 0.9 — slightly higher than `intent_parser` (0) to encourage more natural, conversational tone while remaining coherent.

---

### Task 2: Add `LLMUnavailableError` and 503 handler to `src/kebi/api/errors.py`

Add a custom exception class and register it in `register_error_handlers()`:

```python
class LLMUnavailableError(Exception):
    """Raised when the LLM call fails or times out."""

# In register_error_handlers():
@app.exception_handler(LLMUnavailableError)
def llm_unavailable_handler(request: Request, exc: LLMUnavailableError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"error_type": "llm_unavailable", "detail": str(exc)},
    )
```

---

### Task 3: Create `src/kebi/api/schemas/chat_assistant.py`

```python
class ChatRequest(BaseModel):
    user_id: str
    message: str

class ChatResponse(BaseModel):
    response: str
```

`message` must be non-empty — FastAPI/Pydantic returns 422 automatically for missing/blank fields via `min_length=1` validator.

---

### Task 4: Create `src/kebi/core/chat/__init__.py` and `chat_assistant_service.py`

**System prompt** (persona as defined in spec FR-007):

```
You are a knowledgeable food and dining advisor with deep expertise in global food
culture, cuisines, restaurants, street food, and dining etiquette.

You give direct, opinionated answers. When asked for a recommendation, you make one
— you don't list options without committing to a favourite. When asked a factual
question, you answer it confidently and concisely. You never produce generic
travel-guide language ("there's something for everyone", "it depends on your taste").

Your areas of expertise:
- Destination food scenes (cities, regions, neighbourhoods)
- Food culture and culinary knowledge (ingredients, techniques, dish types, cuisines)
- Dining etiquette and practical advice (tipping, street food safety, reservation customs)
- How to find good places and avoid tourist traps

Be conversational. Be specific. Be useful.
```

**Service pattern** (following `IntentParser` and `OpenAIVisionExtractor` conventions):
- Constructor calls `get_llm("chat_assistant")` and stores client
- `async def run(self, message: str, user_id: str) -> str`
- Wraps call in Langfuse generation: `lf.generation(name="chat_assistant", input={...})`
- On any exception from the LLM call, raises `LLMUnavailableError`

---

### Task 5: Create `src/kebi/api/routes/chat_assistant.py`

```python
@router.post("/chat-assistant", status_code=200, response_model=ChatResponse)
async def chat_assistant(
    body: ChatRequest,
    service: ChatAssistantService = Depends(get_chat_assistant_service),
) -> ChatResponse:
    response = await service.run(body.message, body.user_id)
    return ChatResponse(response=response)
```

---

### Task 6: Add `get_chat_assistant_service()` to `src/kebi/api/deps.py`

```python
def get_chat_assistant_service() -> ChatAssistantService:
    return ChatAssistantService()
```

Simple — no DB, no Redis, no extra deps.

---

### Task 7: Register router in `src/kebi/api/main.py`

```python
from kebi.api.routes.chat_assistant import router as chat_assistant_router
router.include_router(chat_assistant_router, prefix="")
```

---

### Task 8: Create unit tests in `tests/core/chat/test_chat_assistant_service.py`

Three test cases:
1. **Happy path**: mocked LLM returns a string → `service.run()` returns that string
2. **LLM failure**: mocked LLM raises an exception → `service.run()` raises `LLMUnavailableError`
3. **Langfuse tracing**: verify `generation.end()` is called (mock `get_langfuse_client`)

Use `unittest.mock.AsyncMock` for the LLM client's `complete()` method. Patch `get_llm` and `get_langfuse_client` at module level.

---

## Done Criteria

- [ ] `ChatAssistantService.run(message, user_id)` returns a string response
- [ ] `POST /v1/chat-assistant` is reachable and returns `{"response": "..."}``
- [ ] LLM failure raises `LLMUnavailableError` → route returns HTTP 503
- [ ] Empty message rejected with HTTP 422
- [ ] `poetry run pytest tests/core/chat/` passes
- [ ] `poetry run ruff check src/` passes
- [ ] `poetry run mypy src/` passes

## Risks & Notes

- **`config/app.yaml` vs `config/models.yaml`**: The constitution references `config/models.yaml` but the file is actually `config/app.yaml` — all model roles live there under `models:`. This plan follows the actual repo state.
- **Langfuse None-safety**: `get_langfuse_client()` returns `None` when not configured. Service must guard with `if lf:` before calling `.generation()`, matching the pattern in `IntentParser`.
- **LLMUnavailableError import**: Route handler and service must import from `kebi.api.errors` — avoid circular imports by importing only in route file (errors.py has no FastAPI route imports).
