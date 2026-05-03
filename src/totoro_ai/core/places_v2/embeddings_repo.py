"""EmbeddingsRepo — sole writer/reader of the place_embeddings_v2 DB table.

One vector per place_id (UNIQUE FK → places_v2.id, ON DELETE CASCADE). The
repo is purely persistence: no embedding model awareness, no text building.
The caller (EmbeddingService) owns those concerns.

Each row carries a `text_hash` of the source string the vector was built
from. The service uses it to short-circuit re-embedding when neither the
input text nor the model has changed.
"""

from __future__ import annotations

from uuid import uuid4

from pgvector.sqlalchemy import Vector  # type: ignore[import-untyped]
from sqlalchemy import (
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    delete,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

# Embedding dimensions — keep in sync with the configured embedder model.
# Voyage 4-lite emits 1024-dim vectors (ADR-040).
EMBEDDING_DIMENSIONS: int = 1024

# ---------------------------------------------------------------------------
# Table reference — typed columns for native query building
# ---------------------------------------------------------------------------
_metadata = MetaData()
_PlaceEmbeddingsTable = Table(
    "place_embeddings_v2",
    _metadata,
    Column("id", String),
    Column("place_id", String),
    Column("vector", Vector(EMBEDDING_DIMENSIONS)),
    Column("model_name", String),
    Column("text_hash", String),
    Column("created_at", DateTime(timezone=True)),
)
_t = _PlaceEmbeddingsTable.c


class EmbeddingsRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_by_place_ids(
        self, place_ids: list[str]
    ) -> dict[str, list[float]]:
        """Return {place_id: vector} for the given place_ids. Missing → absent."""
        if not place_ids:
            return {}
        stmt = select(_t.place_id, _t.vector).where(_t.place_id.in_(place_ids))
        result = await self._session.execute(stmt)
        return {row.place_id: list(row.vector) for row in result}

    async def get_signatures_by_place_ids(
        self, place_ids: list[str]
    ) -> dict[str, tuple[str, str]]:
        """Return {place_id: (text_hash, model_name)} for diff checks.

        Used by the service to skip re-embedding rows whose source text and
        model both already match what's in the DB.
        """
        if not place_ids:
            return {}
        stmt = select(_t.place_id, _t.text_hash, _t.model_name).where(
            _t.place_id.in_(place_ids)
        )
        result = await self._session.execute(stmt)
        return {row.place_id: (row.text_hash, row.model_name) for row in result}

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def upsert_embeddings(
        self, records: list[tuple[str, list[float], str, str]]
    ) -> None:
        """Bulk INSERT ... ON CONFLICT (place_id) DO UPDATE.

        ``records`` is a list of ``(place_id, vector, model_name, text_hash)``
        tuples. One round-trip regardless of size. Empty list is a no-op.
        """
        if not records:
            return

        rows = [
            {
                "id": str(uuid4()),
                "place_id": pid,
                "vector": vec,
                "model_name": model,
                "text_hash": text_hash,
                "created_at": func.now(),
            }
            for pid, vec, model, text_hash in records
        ]

        insert_stmt = pg_insert(_PlaceEmbeddingsTable).values(rows)
        excl = insert_stmt.excluded
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=["place_id"],
            set_={
                "vector": excl.vector,
                "model_name": excl.model_name,
                "text_hash": excl.text_hash,
                "created_at": func.now(),
            },
        )
        await self._session.execute(stmt)
        await self._session.commit()

    async def delete_by_place_ids(self, place_ids: list[str]) -> int:
        """Delete embeddings for the given place_ids. Returns rows deleted."""
        if not place_ids:
            return 0
        stmt = delete(_PlaceEmbeddingsTable).where(_t.place_id.in_(place_ids))
        result = await self._session.execute(stmt)
        await self._session.commit()
        return result.rowcount or 0
