# Implementation Plan: User Memory Layer

**Branch**: `018-user-memory-layer` | **Date**: 2026-04-10 | **Spec**: [spec.md](spec.md)

## Summary

Add a user memory layer that extracts declarative personal facts from every user message, stores them asynchronously, and injects them into consult and chat assistant pipelines. Fact extraction is added to the existing `intent_router` LLM call (no new model call). Storage is a new `user_memories` table with a `UserMemoryRepository` Protocol. Injection is via parameter threading into `IntentParser.parse()` and `ChatAssistantService.run()`.

## Technical Context

**Language/Version**: Python 3.11  
**Primary Dependencies**: FastAPI 0.115, Pydantic 2.10, SQLAlchemy async, Alembic, Langfuse, Instructor (for IntentParser)  
**Storage**: PostgreSQL (`user_memories` table via Alembic)  
**Testing**: pytest, asyncio_mode=auto  
**Target Platform**: Linux (Railway)  
**Project Type**: web-service  
**Performance Goals**: Memory load/save must not block request response; save runs via FastAPI BackgroundTasks  
**Constraints**: `mypy --strict`, `ruff check`, ADR-044 prompt injection safety for injected memories  
**Scale/Scope**: One new table, ~8 files modified, ~4 new files

## Constitution Check

| Gate | Status | Notes |
|------|--------|-------|
| Repo boundary (AI/ML only, no product CRUD) | PASS | `user_memories` is AI/inference data owned by this repo |
| ADR-001 src layout | PASS | New modules under `src/kebi/core/memory/` |
| ADR-002 hybrid directory | PASS | `core/memory/` follows existing pattern |
| ADR-003 Ruff + mypy strict | PASS | All new code must comply |
| ADR-004 pytest tests | PASS | New test files required per new module |
| ADR-010 LangGraph node isolation | PASS (adapted) | ConsultService is sequential; spirit of ADR-010 applied: memories not passed beyond ranking step |
| ADR-017 Pydantic at all boundaries | PASS | `PersonalFact`, `UserMemory` (ORM), `MemoryConfidenceConfig` all Pydantic |
| ADR-019 FastAPI Depends | PASS | `UserMemoryRepository` injected via `Depends` |
| ADR-020 No hardcoded model names | PASS | No new model role needed; fact extraction added to existing `intent_router` prompt |
| ADR-025 Langfuse on every LLM call | PASS | Extraction runs inside already-traced `classify_intent()` generation |
| ADR-038 Protocol for swappable deps | PASS | `UserMemoryRepository` is a Protocol with Null + SQLAlchemy impls |
| ADR-043 Event dispatcher | PASS | `PersonalFactsExtracted` event follows existing pattern |
| ADR-044 Prompt injection mitigation | **REQUIRED** | Injected memories must be wrapped in XML tags + defensive system prompt instruction in both `IntentParser` and `ChatAssistantService` |
| Constitution VI: DB write ownership | PASS | `user_memories` table owned by this repo; Alembic migration |
| Constitution X: Git commits | PASS | `;` comment char |

**Post-design re-check**: No violations. ADR-044 compliance is a hard requirement — see injection contracts below.

## Project Structure

### Documentation (this feature)

```text
specs/018-user-memory-layer/
├── plan.md              ← this file
├── research.md          ← Phase 0 output
├── data-model.md        ← Phase 1 output
├── contracts/           ← Phase 1 output
│   └── internal-interfaces.md
└── tasks.md             ← Phase 2 output (/speckit.tasks)
```

### Source Code (repository root)

```text
src/kebi/
├── core/
│   └── memory/                          ← NEW module
│       ├── __init__.py
│       ├── schemas.py                   ← PersonalFact Pydantic model
│       ├── repository.py                ← UserMemoryRepository Protocol + impls
│       └── service.py                   ← UserMemoryService (sole consumer of SQLAlchemyUserMemoryRepository)
├── db/
│   └── models.py                        ← + UserMemory SQLAlchemy model
├── core/
│   ├── config.py                        ← + MemoryConfig, MemoryConfidenceConfig
│   ├── chat/
│   │   ├── router.py                    ← extend IntentClassification + system prompt
│   │   ├── service.py                   ← inject EventDispatcher + UserMemoryRepository
│   │   └── chat_assistant_service.py    ← accept user_memories param; ADR-044 injection
│   ├── consult/
│   │   └── service.py                   ← accept user_memories param; pass to IntentParser
│   ├── intent/
│   │   └── intent_parser.py             ← accept user_memories param; ADR-044 injection
│   └── events/
│       ├── events.py                    ← + PersonalFactsExtracted event
│       └── handlers.py                  ← + on_personal_facts_extracted handler
└── api/
    └── deps.py                          ← wire UserMemoryRepository + event handler

alembic/versions/
└── <hash>_add_user_memories_table.py    ← NEW migration

config/
└── app.yaml                             ← + memory.confidence.stated/inferred

tests/
├── core/
│   └── memory/
│       ├── test_schemas.py              ← PersonalFact validation
│       └── test_repository.py           ← SQLAlchemy save/load/dedup
├── core/chat/
│   └── test_router.py                   ← personal_facts in classification output
└── integration/
    └── test_memory_e2e.py               ← full flow: extract → store → inject
```

## Complexity Tracking

No constitution violations requiring justification.

## Implementation Checklist

### Phase A — Foundation (no behaviour change)

- [ ] Add `memory.confidence.stated` and `memory.confidence.inferred` to `config/app.yaml`
- [ ] Add `MemoryConfidenceConfig` and `MemoryConfig` to `core/config.py`; wire into `AppConfig`
- [ ] Create `src/kebi/core/memory/__init__.py` (empty)
- [ ] Create `src/kebi/core/memory/schemas.py` — `PersonalFact` Pydantic model
- [ ] Create `src/kebi/core/memory/repository.py` — `UserMemoryRepository` Protocol + `NullUserMemoryRepository` + `SQLAlchemyUserMemoryRepository`
- [ ] Create `src/kebi/core/memory/service.py` — `UserMemoryService` wrapping the repo; exposes `save_facts()` and `load_memories()`; only class that instantiates `SQLAlchemyUserMemoryRepository` (via `api/deps.py`)
- [ ] Add `UserMemory` SQLAlchemy ORM model to `db/models.py`
- [ ] Generate Alembic migration for `user_memories` table

### Phase B — Extraction pipeline

- [ ] Add `PersonalFact` import to `core/chat/router.py`; extend `IntentClassification` with `personal_facts: list[PersonalFact]`
- [ ] Update `_SYSTEM_PROMPT` in `core/chat/router.py` to instruct LLM to extract user facts (not place attributes); update JSON schema comment
- [ ] Add `PersonalFactsExtracted` event to `core/events/events.py`
- [ ] Add `on_personal_facts_extracted` handler to `core/events/handlers.py`; reads confidence from config; calls `UserMemoryRepository.save()` per fact; skips dups; logs errors, never raises
- [ ] Add `memory_service: UserMemoryService` dep to `EventHandlers.__init__()` — handler calls `memory_service.save_facts()`, never the repo directly
- [ ] Register `personal_facts_extracted` handler in `get_event_dispatcher()` in `api/deps.py`
- [ ] Add `UserMemoryService` dep to `get_event_dispatcher()` (injected into `EventHandlers` via `Depends(get_user_memory_service)`)

### Phase C — Injection pipeline

- [ ] Update `IntentParser.parse()` signature: `user_memories: list[str] | None = None`; inject with XML tags per ADR-044 when not None/empty
- [ ] Update `ConsultService.consult()` signature: `user_memories: list[str] | None = None`; pass to `self._intent_parser.parse()`; set local `user_memories = None` after ranking step
- [ ] Update `ChatAssistantService.run()` signature: `user_memories: list[str] | None = None`; inject into system prompt with XML tags + defensive instruction per ADR-044
- [ ] Update `ChatService.__init__()`: add `memory_service: UserMemoryService` and `event_dispatcher: EventDispatcherProtocol` deps
- [ ] Update `ChatService.run()`: after `classify_intent()`, fire `PersonalFactsExtracted` event via dispatcher
- [ ] Update `ChatService._dispatch()`: for `consult` and `assistant` intents, call `memory_service.load_memories(user_id)` before dispatching; pass result to consult/assistant service; for `extract-place` and `recall`, do not call `load_memories()`
- [ ] Update `get_chat_service()` in `api/deps.py` to inject `UserMemoryService` and `EventDispatcher`
- [ ] Add `get_user_memory_service()` FastAPI dependency to `api/deps.py` — only place `SQLAlchemyUserMemoryRepository` is instantiated

### Phase D — Tests

- [ ] `tests/core/memory/test_schemas.py` — PersonalFact validation, source enum
- [ ] `tests/core/memory/test_repository.py` — save, load, dedup (exact match), NullRepository no-ops
- [ ] `tests/core/chat/test_router.py` — classify_intent returns personal_facts list (may be empty)
- [ ] `tests/core/events/test_handlers.py` — on_personal_facts_extracted writes with correct confidence; skips duplicates; handles empty list
- [ ] `tests/integration/test_memory_e2e.py` — full flow: message with personal fact → PersonalFactsExtracted fired → row in user_memories → load_memories returns it → next consult call receives it

### Phase E — Verify

- [ ] `poetry run pytest` — all pass
- [ ] `poetry run ruff check src/ tests/` — no issues
- [ ] `poetry run mypy src/` — no errors
- [ ] Alembic migration runs clean: `alembic upgrade head`
