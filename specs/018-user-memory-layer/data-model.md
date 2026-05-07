# Data Model: User Memory Layer (018)

## New Entity: UserMemory (SQLAlchemy ORM + PostgreSQL)

**Table**: `user_memories`  
**File**: `src/kebi/db/models.py` (append to existing file)

| Column | Type | Nullable | Constraints | Notes |
|--------|------|----------|-------------|-------|
| `id` | `UUID` (String PK) | No | PK, default `uuid4()` | Consistent with ConsultLog pattern |
| `user_id` | `String` | No | Indexed | Caller-provided user identity |
| `memory` | `Text` | No | — | Plain-text declarative fact |
| `source` | `String` | No | `"stated"` or `"inferred"` | Stored as String, not Enum |
| `confidence` | `Float` | No | — | `0.9` for stated, `0.6` for inferred |
| `created_at` | `DateTime(timezone=True)` | No | `server_default=func.now()` | Append-only; no `updated_at` |

**Constraints**:
- `UniqueConstraint("user_id", "memory", name="uq_user_memories_user_memory")` — dedup at DB level
- No foreign key on `user_id` — this repo does not own the `users` table (Constitution VI)

**No state transitions** — append-only. No delete. No update.

---

## New Schema: PersonalFact (Pydantic)

**File**: `src/kebi/core/memory/schemas.py`

```python
from typing import Literal
from pydantic import BaseModel

class PersonalFact(BaseModel):
    text: str
    source: Literal["stated", "inferred"]
```

**Validation rules**:
- `text` must be non-empty (Pydantic default `str` validation)
- `source` constrained to `"stated"` or `"inferred"` via `Literal`

---

## Extended Schema: IntentClassification (modified)

**File**: `src/kebi/core/chat/router.py`

```python
class IntentClassification(BaseModel):
    intent: str
    confidence: float
    clarification_needed: bool
    clarification_question: str | None = None
    personal_facts: list[PersonalFact] = []   ← ADDED
```

**Backward compatibility**: `personal_facts` defaults to `[]`. If the LLM returns JSON without this field, Pydantic fills the default.

---

## New Event: PersonalFactsExtracted (Pydantic + DomainEvent)

**File**: `src/kebi/core/events/events.py`

```python
class PersonalFactsExtracted(DomainEvent):
    event_type: str = "personal_facts_extracted"
    personal_facts: list[PersonalFact]
    # user_id inherited from DomainEvent
```

**Fired**: After every `classify_intent()` call in `ChatService.run()`.  
**Payload**: `user_id` from request, `personal_facts` from `IntentClassification` (may be empty list).

---

## New Config: MemoryConfig (Pydantic)

**File**: `src/kebi/core/config.py`

```python
class MemoryConfidenceConfig(BaseModel):
    stated: float = 0.9
    inferred: float = 0.6

class MemoryConfig(BaseModel):
    confidence: MemoryConfidenceConfig = MemoryConfidenceConfig()
```

**AppConfig extension**: `memory: MemoryConfig = MemoryConfig()`  
**app.yaml addition**:
```yaml
memory:
  confidence:
    stated: 0.9
    inferred: 0.6
```

---

## Repository Protocol: UserMemoryRepository

**File**: `src/kebi/core/memory/repository.py`

```python
class UserMemoryRepository(Protocol):
    async def save(
        self, user_id: str, memory: str, source: str, confidence: float
    ) -> None: ...

    async def load(self, user_id: str) -> list[str]: ...
```

**Implementations**:

| Class | Behaviour |
|-------|-----------|
| `NullUserMemoryRepository` | `save()` no-ops, `load()` returns `[]` |
| `SQLAlchemyUserMemoryRepository` | `save()` uses INSERT ON CONFLICT DO NOTHING; `load()` returns `memory` strings ordered by `created_at` ASC |

> **Access constraint**: `SQLAlchemyUserMemoryRepository` is instantiated only inside `UserMemoryService`. No other class holds a direct reference to it.

---

## Service: UserMemoryService

**File**: `src/kebi/core/memory/service.py`

```python
class UserMemoryService:
    """Single consumer of UserMemoryRepository.

    All other components (ChatService, EventHandlers) use this service —
    never the repository implementation directly.
    """

    def __init__(self, repo: UserMemoryRepository) -> None: ...

    async def save_facts(
        self,
        user_id: str,
        facts: list[PersonalFact],
        confidence_config: MemoryConfidenceConfig,
    ) -> None:
        """Persist extracted facts. Skips write if facts list is empty.
        Assigns confidence from config by source. Duplicate rows silently skipped."""

    async def load_memories(self, user_id: str) -> list[str]:
        """Return all stored memory strings for user_id.
        Returns [] on failure — never raises."""
```

**Wiring in `api/deps.py`**:
```python
def get_user_memory_service(
    db_session: AsyncSession = Depends(get_session),
) -> UserMemoryService:
    return UserMemoryService(repo=SQLAlchemyUserMemoryRepository(db_session))
```

`SQLAlchemyUserMemoryRepository` is constructed inside this single factory function and passed into `UserMemoryService`. No other dependency function instantiates the repository.

---

## Alembic Migration

**File**: `alembic/versions/<hash>_add_user_memories_table.py`

**Up**:
```sql
CREATE TABLE user_memories (
    id VARCHAR PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    memory TEXT NOT NULL,
    source VARCHAR NOT NULL,
    confidence FLOAT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_user_memories_user_memory UNIQUE (user_id, memory)
);
CREATE INDEX ix_user_memories_user_id ON user_memories (user_id);
```

**Down**: `DROP TABLE user_memories;`

---

## Entity Relationships

```
UserMemory ←→ (no FK to users — cross-repo boundary, Constitution VI)
UserMemory.user_id matches Place.user_id by convention only
PersonalFact (in-memory Pydantic) → serialized as UserMemory row on persist
PersonalFactsExtracted event carries list[PersonalFact] → handler persists to UserMemory
```
