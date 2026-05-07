# Implementation Plan: Streaming Recommendations via SSE

**Branch**: `001-consult-streaming` | **Date**: 2026-03-17 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/001-consult-streaming/spec.md`

## Summary

Add SSE streaming mode to `POST /v1/consult`. When the request includes `"stream": true`, call the configured AI provider (via provider abstraction, role `orchestrator`) with a hardcoded system prompt and stream each token back as `data: {"token": "..."}` SSE events. When all tokens are emitted, send `data: {"done": true}`. When `stream` is absent or false, return the existing synchronous JSON stub. Phase 1 implementation — no LangGraph, no intent parsing, no database. Real pipeline connects in Phase 4.

## Technical Context

**Language/Version**: Python 3.11
**Primary Dependencies**: FastAPI, Starlette (StreamingResponse), pytest, httpx
**Storage**: N/A (Phase 1 — no DB or Redis access)
**Testing**: pytest + httpx.AsyncClient (SSE streaming tests), Anthropic SDK mocked in unit tests
**Target Platform**: Linux server (Railway)
**Project Type**: web-service
**Performance Goals**: First token < 1 second; continuous token stream with no perceptible gaps
**Constraints**: No memory leaks on client disconnect; backward-compatible (stream=false unchanged)
**Scale/Scope**: Single endpoint modification + provider abstraction wiring; clean Phase 4 upgrade path

## Constitution Check

*Pre-implementation gate — all items must pass.*

| ADR | Check | Status |
|-----|-------|--------|
| ADR-001 | Code lives under `src/kebi/` | ✅ Pass |
| ADR-002 | Route in `api/routes/`, service in `core/consult/` | ✅ Pass |
| ADR-003 | Ruff + mypy --strict required before merge | ✅ Pass (enforced in Done criteria) |
| ADR-004 | Tests in `tests/` mirroring `src/kebi/` | ✅ Pass |
| ADR-014 | `/v1` prefix via APIRouter loaded from app.yaml | ✅ Pass |
| ADR-017 | Pydantic models for all request/response schemas | ✅ Pass |
| ADR-018 | Separate router module: `routes/consult.py` | ✅ Pass |
| ADR-019 | FastAPI `Depends()` for DB/Redis | ✅ N/A (Phase 1 has no DB/Redis) |
| ADR-020 | Provider abstraction — `get_llm("orchestrator")`, no hardcoded model names | ✅ Pass (required — real AI call) |
| ADR-021 | consult uses LangGraph StateGraph | ✅ N/A (Phase 1; LangGraph in Phase 4) |
| ADR-025 | Langfuse on every LLM call | ✅ Pass (required — real AI call must be traced) |
| ADR-034 | Route handler is a facade (one service call only) | ✅ Pass |
| ADR-038 | Protocol abstraction for swappable dependencies | ✅ Pass (LLM client resolved via provider layer) |
| X → Constitution | Streaming via raw StreamingResponse, no decorator | ✅ Pass (no sse-starlette or similar) |

**Post-design re-check**: All gates still pass after Phase 1 design. No violations introduced.

## Project Structure

### Documentation (this feature)

```text
specs/001-consult-streaming/
├── plan.md              ← this file
├── spec.md              ← feature specification
├── research.md          ← Phase 0: SSE patterns, client disconnect handling
├── data-model.md        ← Phase 1: request/response schemas
├── contracts/
│   └── consult-stream.md   ← Phase 1: API contract (streaming + sync modes)
├── checklists/
│   └── requirements.md  ← quality checklist
└── tasks.md             ← Phase 2 output (/speckit.tasks — not yet created)
```

### Source Code (repository root)

```text
src/kebi/
├── api/
│   ├── main.py                    ← MODIFY: include consult router
│   ├── schemas/                   ← NEW
│   │   ├── __init__.py
│   │   └── consult.py             ← Pydantic request/response models
│   └── routes/                    ← NEW
│       ├── __init__.py
│       └── consult.py             ← Route handler (facade, <30 lines)
└── core/
    └── consult/                   ← NEW
        ├── __init__.py
        └── service.py             ← ConsultService with sync + streaming paths

tests/
├── api/                           ← NEW
│   ├── __init__.py
│   └── test_consult.py            ← Integration tests (sync + streaming modes)
└── core/
    └── consult/                   ← NEW
        ├── __init__.py
        └── test_service.py        ← Unit tests for ConsultService

kebi-config/bruno/ai-service/
└── consult-stream.bru             ← NEW: Bruno request file for streaming mode
```

**Structure Decision**: Single project layout (ADR-001/ADR-002). Routes in `api/routes/`, schemas in `api/schemas/`, business logic in `core/consult/`. Mirrors the established pattern for separation of concerns (ADR-034).

## Complexity Tracking

No constitution violations. No complexity justification needed.

---

## Phase 0: Research ✅ Complete

See [research.md](research.md) for full findings.

**Key decisions:**
- `StreamingResponse` with async generator — no third-party SSE library (FR-013)
- `request.is_disconnected()` + `try/finally` = clean disconnect + resource cleanup (FR-009, FR-010)
- SSE format: `data: {"token": "..."}\n\n` per token; `data: {"done": true}\n\n` as final event
- Headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no`
- `get_llm("orchestrator")` resolves Anthropic client via provider abstraction (ADR-020)
- Langfuse callback attached to AI call for tracing (ADR-025)
- Testing: mock Anthropic client in unit tests; `httpx.AsyncClient.aiter_lines()` for integration SSE tests

---

## Phase 1: Design & Contracts ✅ Complete

### Data Model

See [data-model.md](data-model.md).

**New/modified schemas:**
- `ConsultRequest` — adds optional `stream: bool = False` field
- `SyncConsultResponse` — existing synchronous response shape (unchanged)
- `TokenEvent` — SSE token payload: `{"token": "..."}`
- `DoneEvent` — SSE terminal payload: `{"done": true}`

No new DB tables. Streaming is a transport change plus a real AI call.

### API Contract

See [contracts/consult-stream.md](contracts/consult-stream.md).

**Contract v1.1 changes:**
- `POST /v1/consult` accepts optional `"stream": true`
- Streaming response: `text/event-stream`, one `data: {"token": "..."}` per AI token + final `data: {"done": true}`
- Synchronous response unchanged
- Fully backward-compatible (no breaking change)

### Implementation Notes

**`src/kebi/api/schemas/consult.py`**
```python
from pydantic import BaseModel

class Location(BaseModel):
    lat: float
    lng: float

class ConsultRequest(BaseModel):
    user_id: str
    query: str
    location: Location | None = None
    stream: bool = False

class PlaceResult(BaseModel):
    place_name: str
    address: str
    reasoning: str
    source: str  # "saved" | "discovered"

class ReasoningStep(BaseModel):
    step: str
    summary: str

class SyncConsultResponse(BaseModel):
    primary: PlaceResult
    alternatives: list[PlaceResult]
    reasoning_steps: list[ReasoningStep]
```

**`src/kebi/core/consult/service.py`**
```python
import json
from collections.abc import AsyncGenerator

class ConsultService:
    def __init__(self, llm: LLMClientProtocol) -> None:
        self._llm = llm

    async def consult(self, user_id: str, query: str, ...) -> SyncConsultResponse:
        """Synchronous stub for Phase 1."""
        ...

    async def stream(
        self, user_id: str, query: str, request: Request, ...
    ) -> AsyncGenerator[str, None]:
        """Stream tokens from AI provider (Phase 1: hardcoded system prompt)."""
        try:
            async with self._llm.stream(SYSTEM_PROMPT, query) as ai_stream:
                async for token in ai_stream:
                    if await request.is_disconnected():
                        break
                    yield f'data: {json.dumps({"token": token})}\n\n'
            if not await request.is_disconnected():
                yield f'data: {json.dumps({"done": True})}\n\n'
        except GeneratorExit:
            pass
        finally:
            pass  # AI stream closed via async context manager
```

**`src/kebi/api/routes/consult.py`**
```python
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

router = APIRouter()

def get_consult_service() -> ConsultService:
    return ConsultService(llm=get_llm("orchestrator"))

@router.post("/consult")
async def consult(
    body: ConsultRequest,
    raw_request: Request,
    service: ConsultService = Depends(get_consult_service),
) -> Response:
    if body.stream:
        return StreamingResponse(
            service.stream(body.user_id, body.query, raw_request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    result = await service.consult(body.user_id, body.query, body.location)
    return JSONResponse(result.model_dump())
```

---

## Verify Commands

Run these before declaring the feature done:

```bash
poetry run pytest tests/api/test_consult.py tests/core/consult/test_service.py -v
poetry run ruff check src/ tests/
poetry run ruff format src/ tests/ --check
poetry run mypy src/
```

---

## Done Criteria (from spec)

- [ ] `POST /v1/consult` with `"stream": true` streams real Anthropic tokens to Bruno as SSE events
- [ ] `POST /v1/consult` without `"stream"` still returns synchronous JSON stub
- [ ] Client disconnect cleans up the async generator without leaking memory
- [ ] `poetry run pytest tests/core/consult/` passes
- [ ] `poetry run ruff check src/` passes
- [ ] `poetry run mypy src/` passes
