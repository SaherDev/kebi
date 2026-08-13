"""Index knowledge_claims.source_ref for author-scoped reads (curation).

Curated claims are global rows (user_id NULL); their author is recorded only
in source_ref ("curator:{user_id}"). The claims-management endpoints — list
my claims, retract one — filter on that column, which until now had no
index. Partial: most rows carry a source_ref, but NULLs buy nothing.
"""

import sqlalchemy as sa
from alembic import op

revision = "b7c8d9e0f1a2"
down_revision = "z4a5b6c7d8e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_knowledge_claims_source_ref",
        "knowledge_claims",
        ["source_ref"],
        postgresql_where=sa.text("source_ref IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_claims_source_ref", table_name="knowledge_claims")
