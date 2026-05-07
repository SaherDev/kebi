# Data Model: Voyage Embedding Pipeline

**Branch**: `005-voyage-embed-pipeline` | **Date**: 2026-03-30

---

## Existing Entities (Unchanged)

### Place (existing, `places` table)

No schema changes. The place entity already contains all fields needed to construct the embedding input text.

Key fields used for embedding input:
- `id: str` — UUID string, used as `place_id` when writing the embedding
- `place_name: str` — primary text signal
- `cuisine: str | None` — category signal
- `address: str` — geographic signal

---

### Embedding (existing, `embeddings` table)

No schema changes. The column dimension is already `VECTOR(1024)` per migration `f7cf21e03bc7`. The model already defines `EMBEDDING_DIMENSIONS = 1024`.

```
embeddings
├── id: str (UUID, primary key)
├── place_id: str (FK → places.id, CASCADE DELETE, indexed)
├── vector: Vector(1024) (NOT NULL)
├── model_name: str (NOT NULL) — e.g. "voyage-4-lite"
└── created_at: datetime (server default)
```

**Upsert key**: `place_id` — one embedding per place. Upsert = delete existing row for `place_id`, insert fresh row with new vector.

---

## New Abstractions (No DB Schema Changes)

### EmbedderProtocol

An async interface for any embedding provider. Lives in `src/kebi/providers/embeddings.py`.

```
EmbedderProtocol
└── embed(texts: list[str], input_type: str) -> list[list[float]]
    ├── texts: one or more text strings to embed
    ├── input_type: "document" (place saves) | "query" (recall/search)
    └── returns: list of 1024-dimensional float vectors (one per input text)
```

### VoyageEmbedder

Concrete implementation of `EmbedderProtocol` wrapping the voyageai async client.

```
VoyageEmbedder
├── _model: str (loaded from config, e.g. "voyage-4-lite")
├── _client: voyageai.AsyncClient
└── embed(texts, input_type) -> list[list[float]]
    └── wraps voyageai call with Langfuse generation span
```

### EmbeddingRepository (Protocol)

Async interface for embedding persistence. Lives in `src/kebi/db/repositories/embedding_repository.py`.

```
EmbeddingRepository
└── upsert_embedding(place_id: str, vector: list[float], model_name: str) -> None
    ├── place_id: UUID string matching places.id
    ├── vector: 1024-dimensional float list
    └── model_name: model identifier for traceability
```

### SQLAlchemyEmbeddingRepository

Concrete implementation of `EmbeddingRepository`.

```
SQLAlchemyEmbeddingRepository
├── _session: AsyncSession
└── upsert_embedding(place_id, vector, model_name) -> None
    ├── Query: SELECT * FROM embeddings WHERE place_id = ?
    ├── If found: DELETE existing row
    ├── INSERT new Embedding(id=uuid4(), place_id=..., vector=..., model_name=...)
    └── On error: rollback, log, raise RuntimeError
```

---

## Embedding Input Construction

| Place Fields Available | Embedding Text Format |
|------------------------|----------------------|
| `place_name` only | `"Ramen Nagi"` |
| `place_name` + `address` | `"Ramen Nagi, 123 Main St"` |
| `place_name` + `cuisine` + `address` | `"Ramen Nagi, Japanese, 123 Main St"` |

**input_type at save time**: `"document"`
**input_type at query time**: `"query"` (used by recall/consult pipeline, not this feature)

---

## Service Flow Change

**Before** (current `ExtractionService.run()`):
```
validate → dispatch → places_validate → confidence → threshold → dedup → save_place → return
```

**After** (with embedding):
```
validate → dispatch → places_validate → confidence → threshold → dedup → save_place → embed → save_embedding → return
```

Embedding only happens on the new-place path (after step 7). The dedup early-return path (step 6) is unchanged — no embedding is generated for existing places.

---

## ExtractionService Constructor Change

```python
; Before
ExtractionService(dispatcher, places_client, place_repo, extraction_config)

; After
ExtractionService(dispatcher, places_client, place_repo, extraction_config, embedder, embedding_repo)
```

`deps.py` provides the two new dependencies via `get_embedder()` and `SQLAlchemyEmbeddingRepository(session)`.

---

## Validation Rules

- `vector` must have exactly 1024 floats — enforced by pgvector column type
- `place_id` must reference an existing row in `places` — enforced by FK constraint
- `model_name` must be non-empty — enforced by `NOT NULL` column constraint
- `texts` passed to embedder must be non-empty list — validated in `VoyageEmbedder.embed()`
