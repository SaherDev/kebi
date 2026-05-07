"""Repository pattern implementation for Embedding model.

Provides Protocol and implementation for database operations on Embedding entities.
Handles upsert semantics with one-embedding-per-place guarantee.
"""

import logging
from typing import Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from kebi.db.models import Embedding

logger = logging.getLogger(__name__)


class EmbeddingRepository(Protocol):
    """Protocol for Embedding repository operations.

    Defines the interface for database access to embeddings. Implementations must:
    - upsert_embedding(): Insert new or replace existing embedding for a place
    - bulk_upsert_embeddings(): Batch upsert multiple embeddings in one round-trip
    """

    async def upsert_embedding(
        self, place_id: str, vector: list[float], model_name: str
    ) -> None:
        """Upsert embedding for a place.

        Implements upsert semantics:
        - If (place_id) exists: delete old row and insert new
        - If not exists: insert new record
        - On error: rollback, log with context, raise RuntimeError

        Args:
            place_id: Place ID (UUID string)
            vector: 1024-dimensional float vector
            model_name: Model identifier (e.g., 'voyage-4-lite')

        Raises:
            RuntimeError: If upsert operation fails
        """
        ...

    async def bulk_upsert_embeddings(
        self,
        records: list[tuple[str, list[float], str]],
    ) -> None:
        """Upsert multiple embeddings in a single SQL statement.

        Args:
            records: List of (place_id, vector, model_name) tuples.
                     All records inserted or updated in one round-trip.
                     Empty list → no-op, no DB call.

        Raises:
            RuntimeError: If the batch operation fails
        """
        ...


class SQLAlchemyEmbeddingRepository:
    """SQLAlchemy implementation of EmbeddingRepository.

    Handles async operations with explicit error recovery (rollback + logging).
    """

    def __init__(self, session: AsyncSession) -> None:
        """Initialize repository with database session.

        Args:
            session: AsyncSession for database operations
        """
        self._session = session

    async def upsert_embedding(
        self, place_id: str, vector: list[float], model_name: str
    ) -> None:
        """Upsert embedding for a place (delete-then-insert pattern).

        Implements upsert logic:
        1. Query existing row by place_id
        2. If found, delete it
        3. Insert new Embedding(id=uuid4(), place_id=..., vector=..., model_name=...)
        4. On any error: rollback, log with context, re-raise as RuntimeError

        Args:
            place_id: Place ID (UUID string)
            vector: 1024-dimensional float vector
            model_name: Model identifier (e.g., 'voyage-4-lite')

        Raises:
            RuntimeError: If upsert fails (includes place_id in message)
        """
        try:
            # Query existing row
            existing = await self._session.scalar(
                select(Embedding).filter_by(place_id=place_id)
            )

            # Delete if exists
            if existing:
                await self._session.delete(existing)

            # Insert new row
            embedding = Embedding(
                id=str(uuid4()),
                place_id=place_id,
                vector=vector,
                model_name=model_name,
            )
            self._session.add(embedding)
            await self._session.commit()

        except Exception as e:
            await self._session.rollback()
            logger.error(
                "Failed to upsert embedding",
                extra={
                    "place_id": place_id,
                    "model_name": model_name,
                    "error": str(e),
                },
            )
            raise RuntimeError(
                f"Failed to upsert embedding for place {place_id}: {e}"
            ) from e

    async def bulk_upsert_embeddings(
        self,
        records: list[tuple[str, list[float], str]],
    ) -> None:
        """Upsert multiple embeddings in a single SQL statement.

        Uses PostgreSQL's INSERT ... ON CONFLICT for efficient batch upsert.
        All records are inserted or updated in a single round-trip.

        Args:
            records: List of (place_id, vector, model_name) tuples.
                     All records inserted or updated in one round-trip.
                     Empty list → no-op, no DB call.

        Raises:
            RuntimeError: If the batch operation fails
        """
        if not records:
            return

        rows = [
            {"id": str(uuid4()), "place_id": pid, "vector": vec, "model_name": model}
            for pid, vec, model in records
        ]

        stmt = pg_insert(Embedding).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["place_id"],
            set_={
                "vector": stmt.excluded.vector,
                "model_name": stmt.excluded.model_name,
            },
        )

        try:
            await self._session.execute(stmt)
            await self._session.commit()
        except Exception as e:
            await self._session.rollback()
            logger.error(
                "Failed to bulk upsert embeddings",
                extra={"record_count": len(records), "error": str(e)},
            )
            raise RuntimeError(
                f"Failed to bulk upsert {len(records)} embeddings: {e}"
            ) from e
