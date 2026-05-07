# Research: Voyage Embedding Pipeline

**Branch**: `005-voyage-embed-pipeline` | **Date**: 2026-03-30

---

## 1. Alembic Migration State

**Decision**: No new migration needed for VECTOR(1024).

**Rationale**: Migration `f7cf21e03bc7_change_embedding_vector_dimension_to_1024` already exists and is in the migration chain (it is a dependency of `a1b2c3d4e5f6`, the current head). The `Embedding` model in `db/models.py` already declares `EMBEDDING_DIMENSIONS = 1024` and `Vector(EMBEDDING_DIMENSIONS)`. Running `alembic upgrade head` is sufficient.

**Verified by**: Reading all migration files and the `Embedding` model definition.

---

## 2. voyageai SDK API (v0.3.7)

**Decision**: Use `voyageai.AsyncClient` for async embedding calls, with `input_type` passed per call.

**Rationale**: The locked version is `voyageai 0.3.7`. The SDK exposes both `Client` (sync) and `AsyncClient` (async). Since `ExtractionService.run()` is fully async, the async client avoids blocking the event loop. The API is:

```python
import voyageai
client = voyageai.AsyncClient(api_key="...")
result = await client.embed(texts, model="voyage-4-lite", input_type="document")
vectors: list[list[float]] = result.embeddings
```

**`input_type` values**: `"document"` for place descriptions (at save time); `"query"` for search queries (at recall/consult time). These produce different embedding vectors optimised for each role — matching `input_type` at query time improves retrieval accuracy.

**Alternatives considered**: `asyncio.run_in_executor` wrapping sync client — rejected, adds executor overhead unnecessarily when async client exists.

---

## 3. Langfuse Tracing for voyageai Calls

**Decision**: Use Langfuse low-level SDK `generation()` API directly (not LangChain callbacks).

**Rationale**: voyageai SDK has no LangChain callback support. The existing `get_langfuse_client()` in `providers/tracing.py` returns a `langfuse.Langfuse()` instance. Use its `generation()` context manager for direct tracing. Follows the graceful-degradation pattern already established: if `get_langfuse_client()` returns `None`, skip tracing silently.

**Pattern**:
```python
lf = get_langfuse_client()
generation = (
    lf.generation(name="voyage_embed", model=self._model, input=texts)
    if lf else None
)
try:
    result = await self._client.embed(texts, model=self._model, input_type=input_type)
    if generation:
        generation.end()
    return result.embeddings
except Exception:
    if generation:
        generation.end(level="ERROR")
    raise
```

**Alternatives considered**: Adding `get_langfuse_handler()` for LangChain callbacks — rejected, voyageai is not a LangChain component. The spec assumption about `get_langfuse_handler()` was incorrect; `get_langfuse_client()` is the right entry point.

---

## 4. EmbedderProtocol Design

**Decision**: Define `EmbedderProtocol` as a `typing.Protocol` with a single async method in `src/kebi/providers/embeddings.py`, co-located with the concrete `VoyageEmbedder` and factory — matching the pattern of `llm.py`.

**Rationale**: Follows the established pattern from `llm.py` (Protocol + concrete class + factory in one file). Service and route code import only the Protocol, never the concrete class. Adding `@runtime_checkable` is consistent with `LLMClientProtocol`.

**Interface**:
```python
class EmbedderProtocol(Protocol):
    async def embed(self, texts: list[str], input_type: str) -> list[list[float]]: ...
```

**Alternatives considered**: Separate files for protocol vs implementation — rejected, unnecessary complexity given a single implementation today. Synchronous protocol — rejected, the extraction service is fully async.

---

## 5. EmbeddingRepository Upsert Design

**Decision**: Use the existing get-then-update ORM pattern (matching `SQLAlchemyPlaceRepository`). Query by `place_id`, delete-then-insert if found, insert-fresh otherwise.

**Rationale**: Per the research from specs/003, `ON CONFLICT DO UPDATE` is avoided in this codebase because it generates raw SQL that bypasses ORM field tracking. The explicit ORM pattern is the established standard. For embeddings, since there's one embedding per place (no dedup key other than `place_id`), the upsert logic is: query for existing row by `place_id`, if found delete the old row and insert a new one (to get a fresh `id`), if not found insert fresh.

**Why delete-then-insert over update-in-place**: The `vector` column is the primary value; replacing a row is semantically cleaner than mutating it. Also avoids stale `model_name` if the embedder is swapped.

**Interface (Protocol)**:
```python
class EmbeddingRepository(Protocol):
    async def upsert_embedding(
        self, place_id: str, vector: list[float], model_name: str
    ) -> None: ...
```

**Note**: `place_id` is `str` (matches the `Place.id` field which is a UUID string).

---

## 6. Wiring into ExtractionService

**Decision**: Add `embedder: EmbedderProtocol` and `embedding_repo: EmbeddingRepository` to `ExtractionService.__init__()`, and call them after `place_repo.save()` in the `run()` method.

**Rationale**: ADR-034 — all orchestration through the service layer. The route handler (`extract_place.py`) must not call the embedder. The `deps.py` dependency factory (`get_extraction_service`) is where concrete implementations are injected.

**Text to embed**: Combine `place_name`, `cuisine` (if present), and `address` as the embedding input. This gives the richest semantic signal. Format: `"{place_name}, {cuisine}, {address}"` (or `"{place_name}, {address}"` if cuisine is None).

**Only embed on new saves**: If the place already exists (dedup path, step 6 early return), skip embedding — the embedding from the original save still applies. Only embed when a new `Place` row is written to the database (step 7 of the current service flow).

**Alternatives considered**: Always re-embed on upsert — rejected, the current dedup path returns before writing any row, so there's nothing new to embed. Re-embedding unchanged places adds cost for no quality gain.

---

## 7. Factory: `get_embedder()`

**Decision**: Add `get_embedder() -> EmbedderProtocol` factory to `src/kebi/providers/embeddings.py`, reading from `get_config().models["embedder"]` and `get_secrets().providers.voyage.api_key`.

**Config entry** (already in `config/app.yaml`):
```yaml
models:
  embedder:
    provider: voyage
    model: voyage-4-lite
    dimensions: 1024
```

**Secrets entry** (in `config/.local.yaml`):
```yaml
providers:
  voyage:
    api_key: "..."
```

**Verified by**: `src/kebi/core/config.py` already handles `voyage` in `_secrets_from_env()` (line 233: `"voyage": {"api_key": os.environ.get("VOYAGE_API_KEY")}`), and `tests/conftest.py` sets `VOYAGE_API_KEY` dummy value.

---

## 8. Description Text Construction

**Decision**: Build embedding input text as: `f"{place_name}, {cuisine}, {address}"` where cuisine is omitted if `None`.

**Rationale**: This is the most information-dense representation of a place using already-extracted structured fields. Place name is the strongest signal; cuisine adds category semantics; address adds geographic context. No LLM call needed — fields are already structured after extraction.

**Implementation**:
```python
parts = [place.place_name]
if place.cuisine:
    parts.append(place.cuisine)
parts.append(place.address)
description = ", ".join(parts)
```
