"""Database repository patterns.

Provides Protocol abstractions and concrete implementations for database access.
"""

from kebi.db.repositories.area_entity_repository import (
    AreaEntityRepository,
    SQLAlchemyAreaEntityRepository,
)
from kebi.db.repositories.knowledge_claim_repository import (
    KnowledgeClaimRepository,
    SQLAlchemyKnowledgeClaimRepository,
)
from kebi.db.repositories.taste_model_repository import (
    SQLAlchemyTasteModelRepository,
    TasteModelRepository,
)

__all__ = [
    "AreaEntityRepository",
    "SQLAlchemyAreaEntityRepository",
    "TasteModelRepository",
    "SQLAlchemyTasteModelRepository",
    "KnowledgeClaimRepository",
    "SQLAlchemyKnowledgeClaimRepository",
]
