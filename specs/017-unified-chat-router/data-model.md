# Data Model: Unified Chat Router

**Branch**: `017-unified-chat-router` | **Date**: 2026-04-09

---

## New: consult_logs Table

Replaces the spec's `recommendations` table name (renamed to avoid conflict with the NestJS-owned `recommendations` table — ADR-052).

| Column | Type | Nullable | Notes |
|---|---|---|---|
| `id` | `UUID` | No | Primary key, server-generated |
| `user_id` | `String` | No | Clerk-issued user identifier; not validated here |
| `query` | `Text` | No | Raw message that produced this consult result |
| `response` | `JSONB` | No | Full `ConsultResponse` payload serialized to JSONB |
| `intent` | `String` | No | Always `"consult"` for rows in this table |
| `accepted` | `Boolean` | Yes | Null until the user signals acceptance/rejection |
| `selected_place_id` | `String` | Yes | FK to `places.id` if user selected a specific place |
| `created_at` | `DateTime(tz=True)` | No | Server default `now()` |

**SQLAlchemy model** → `src/kebi/db/models.py` class `ConsultLog`  
**Alembic migration** → `alembic/versions/<hash>_add_consult_logs_table.py`  
**Repository** → `src/kebi/db/repositories/consult_log_repository.py`

---

## New: Pydantic Schemas

### `src/kebi/api/schemas/chat.py`

```
ChatRequest
├── user_id: str
├── message: str
└── location: Location | None = None   ← reuses Location from schemas/consult.py

ChatResponse
├── type: str    # "extract-place" | "consult" | "recall" | "assistant" | "clarification" | "error"
├── message: str
└── data: dict | None
```

### `src/kebi/core/chat/router.py`

```
IntentClassification  (internal — never crosses API boundary)
├── intent: str          # "extract-place" | "consult" | "recall" | "assistant"
├── confidence: float    # 0.0–1.0
├── clarification_needed: bool
└── clarification_question: str | None
```

---

## Unchanged Tables

The following tables are not modified by this feature:

| Table | Owned by | Notes |
|---|---|---|
| `places` | This repo (Alembic) | No change |
| `embeddings` | This repo (Alembic) | No change |
| `taste_model` | This repo (Alembic) | No change |
| `interaction_log` | This repo (Alembic) | No change |
| `users`, `user_settings` | NestJS (TypeORM) | Never touched by this repo |

---

## Repository Contract

```python
class ConsultLogRepository(Protocol):
    async def save(self, log: ConsultLog) -> None: ...

class SQLAlchemyConsultLogRepository:
    def __init__(self, session: AsyncSession) -> None: ...
    async def save(self, log: ConsultLog) -> None: ...
```

Follows the existing `SQLAlchemy*Repository` pattern in `db/repositories/`.
