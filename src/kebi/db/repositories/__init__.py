"""Database repository patterns.

Provides Protocol abstractions and concrete implementations for database access.
"""

from kebi.db.repositories.embedding_repository import (
    EmbeddingRepository,
    SQLAlchemyEmbeddingRepository,
)
from kebi.db.repositories.taste_model_repository import (
    SQLAlchemyTasteModelRepository,
    TasteModelRepository,
)

__all__ = [
    "EmbeddingRepository",
    "SQLAlchemyEmbeddingRepository",
    "TasteModelRepository",
    "SQLAlchemyTasteModelRepository",
]
