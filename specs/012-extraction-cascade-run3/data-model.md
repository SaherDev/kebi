# Data Model: Extraction Cascade Run 3

**Date**: 2026-04-06
**Branch**: 012-extraction-cascade-run3

No schema migrations in this run — all DB schema changes are deferred to Run 4.
This document describes the **code-layer** entity changes (Pydantic models, dataclasses, Python types).

---

## 1. PlaceSaved (domain event) — MODIFIED

**File**: `src/kebi/core/events/events.py`

```
Before:
  PlaceSaved(DomainEvent)
    event_id: str           [inherited]
    event_type: str = "place_saved"  [inherited]
    user_id: str            [inherited]
    place_id: str           ← REMOVED
    place_metadata: dict

After:
  PlaceSaved(DomainEvent)
    event_id: str           [inherited]
    event_type: str = "place_saved"  [inherited]
    user_id: str            [inherited]
    place_ids: list[str]    ← NEW (was place_id: str)
    place_metadata: dict
```

**Consumers updated atomically (Phase 8)**:
- `events.py` — field definition
- `handlers.py` (`EventHandlers.on_place_saved`) — `event.place_id` → `event.place_ids`
- `taste/service.py` (`TasteModelService.handle_place_saved`) — param `place_id: str` → `place_ids: list[str]`

---

## 2. ExtractionPersistenceService (new service) — CREATED

**File**: `src/kebi/core/extraction/persistence.py`

```
ExtractionPersistenceService
  Constructor:
    place_repo: PlaceRepository
    embedding_repo: EmbeddingRepository
    embedder: EmbedderProtocol
    event_dispatcher: EventDispatcherProtocol

  Methods:
    async save_and_emit(results: list[ExtractionResult], user_id: str) -> list[str]
      ; Returns list of place_id strings for newly written places (excludes deduped)
      ; Ordering invariant: DB writes → PlaceSaved dispatch → embedding loop

    _build_description(result: ExtractionResult) -> str
      ; Reads from ExtractionResult, NOT from Place
      ; parts = [result.place_name, result.cuisine?, result.address?]
      ; Joined with config.embeddings.description_separator
```

**Input/output type**:
- Input: `list[ExtractionResult]` (new dataclass from `types.py`)
- Output: `list[str]` (place UUIDs)

---

## 3. ExtractionService — REWRITTEN

**File**: `src/kebi/core/extraction/service.py`

```
Before (7 deps):
  ExtractionService(
    dispatcher: ExtractionDispatcher,
    places_client: PlacesClient,
    place_repo: PlaceRepository,
    extraction_config: ExtractionConfig,
    embedder: EmbedderProtocol,
    embedding_repo: EmbeddingRepository,
    event_dispatcher: EventDispatcherProtocol,
  )

After (2 deps):
  ExtractionService(
    pipeline: ExtractionPipeline,
    persistence: ExtractionPersistenceService,
  )

  async run(raw_input: str, user_id: str) -> ExtractPlaceResponse
```

**Removed logic** (moved to appropriate owners):
- `_build_description()` → `ExtractionPersistenceService`
- DB writes → `ExtractionPersistenceService.save_and_emit()`
- `compute_confidence()` → `GooglePlacesValidator._validate_one()`
- Threshold checks → `GooglePlacesValidator`
- `PlaceSaved` dispatch → `ExtractionPersistenceService.save_and_emit()`
- Embedding generation → `ExtractionPersistenceService.save_and_emit()`

---

## 4. ExtractPlaceResponse (API schema) — BREAKING CHANGE

**File**: `src/kebi/api/schemas/extract_place.py`

```
Before:
  ExtractPlaceResponse
    place_id: str | None
    place: PlaceExtraction
    confidence: float
    requires_confirmation: bool
    source_url: str | None

After:
  SavedPlace (NEW)
    place_id: str
    place_name: str
    address: str | None
    city: str | None
    cuisine: str | None
    confidence: float
    resolved_by: str
    external_provider: str | None
    external_id: str | None

  ExtractPlaceResponse (REWRITTEN)
    provisional: bool
    places: list[SavedPlace]
    pending_levels: list[str]
    extraction_status: str   ; "saved" | "processing" | "duplicate"
    source_url: str | None
```

**Deleted models**: `PlaceExtraction` — only used by old `ExtractionResult(BaseModel)` (also deleted).

---

## 5. ExtractionPendingHandler — WIRED

**File**: `src/kebi/core/extraction/handlers/extraction_pending.py`

```
Before:
  persistence: Any  (stub with TODO comment)

After:
  persistence: ExtractionPersistenceService  (typed, real call)
```

---

## 6. TasteModelService.handle_place_saved — SIGNATURE CHANGE

**File**: `src/kebi/core/taste/service.py`

```
Before:
  async handle_place_saved(user_id: str, place_id: str, place_metadata: dict) -> None
    ; Logs 1 interaction, runs 1 EMA update

After:
  async handle_place_saved(user_id: str, place_ids: list[str], place_metadata: dict) -> None
    ; Logs N interactions (one per place_id, signal_type=SAVE)
    ; Runs 1 EMA update (one batch, not N separate updates)
    ; interaction_count += len(place_ids) via repository.upsert()
```

---

## 7. Files Deleted (Phase 13)

| File | Why deleted |
|------|-------------|
| `src/kebi/core/extraction/dispatcher.py` | Old routing logic, replaced by ExtractionPipeline |
| `src/kebi/core/extraction/extractors/tiktok.py` | Old TikTok extractor, replaced by enrichers |
| `src/kebi/core/extraction/extractors/plain_text.py` | Old plain text extractor, replaced by enrichers |
| `src/kebi/core/extraction/result.py` | Legacy ExtractionResult(BaseModel), superseded by types.py |

## 8. Symbols Deleted from Existing Files (Phase 13)

| File | Symbol removed | Why |
|------|---------------|-----|
| `confidence.py` | `ExtractionSource` enum | Only used by old dispatcher/extractors |
| `confidence.py` | `compute_confidence()` | Replaced by `calculate_confidence()` (ADR-029) |
| `protocols.py` | `InputExtractor` Protocol | Only used by old dispatcher |
| `api/errors.py` | `ExtractionFailedNoMatchError` | Only used by old threshold logic in service.py |

## 9. EmbeddingRepository — MODIFIED

**File**: `src/kebi/db/repositories/embedding_repository.py`

```
EmbeddingRepository (Protocol) — add:
  async bulk_upsert_embeddings(
      records: list[tuple[str, list[float], str]]
  ) -> None
    ; records: (place_id, vector, model_name) tuples
    ; All records upserted in one SQL round-trip
    ; Empty list → no-op
    ; On error: rollback, log, raise RuntimeError

SQLAlchemyEmbeddingRepository — add:
  Same signature; uses a single INSERT ... ON CONFLICT statement
  or delete-then-insert batch within one transaction
```

Used by: `ExtractionPersistenceService.save_and_emit()` — replaces N individual `upsert_embedding` calls with one bulk call.

---

## 10. No DB migrations

`Place`, `Embedding`, `TasteModel`, `InteractionLog` schemas are unchanged.
`place_ids` lives only in the Python event layer — it is not persisted as a column.
