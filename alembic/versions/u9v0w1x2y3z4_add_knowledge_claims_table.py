"""add knowledge_claims table (ADR-120)

The knowledge layer's substrate: one row per world-knowledge claim,
entity-scoped, generalizing every origin (harvested content, curated
expertise, conversation) behind one shape. `entity_key` is a canonical,
collision-proof identifier (`place:<places.id>` for places, a lowercased
hierarchical geo slug for country/city/neighborhood) so lookups never
cross-contaminate on a bare name. `user_id` is NULL for global claims
(shared_content, curated_expert) and set for conversation-origin claims
(kebi_message, user_message), read back only for that same user — the
privacy boundary lives in the schema. Dedup enforced at the database via a
unique index treating NULLs as not distinct (pg 15+), so a re-harvested
global claim and a re-said user claim both collapse correctly. Pure schema
addition, fully reversible; nothing yet writes to or reads from this table.

Revision ID: u9v0w1x2y3z4
Revises: t8u9v0w1x2y3
Create Date: 2026-07-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "u9v0w1x2y3z4"
down_revision: str | Sequence[str] | None = "t8u9v0w1x2y3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENTITY_TYPE = postgresql.ENUM(
    "country",
    "city",
    "neighborhood",
    "place",
    name="knowledge_entity_type",
)
_SOURCE_TYPE = postgresql.ENUM(
    "shared_content",
    "curated_expert",
    "kebi_message",
    "user_message",
    name="knowledge_source_type",
)


def upgrade() -> None:
    bind = op.get_bind()
    _ENTITY_TYPE.create(bind, checkfirst=True)
    _SOURCE_TYPE.create(bind, checkfirst=True)

    op.create_table(
        "knowledge_claims",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "entity_type",
            _ENTITY_TYPE,
            nullable=False,
        ),
        sa.Column("entity_key", sa.String(), nullable=False),
        sa.Column("entity_name", sa.String(), nullable=False),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column(
            "tags",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "source_type",
            _SOURCE_TYPE,
            nullable=False,
        ),
        sa.Column("source_ref", sa.String(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_claims_entity",
        "knowledge_claims",
        ["entity_type", "entity_key"],
    )
    op.create_index(
        "ix_knowledge_claims_user",
        "knowledge_claims",
        ["user_id"],
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.create_index(
        "uq_knowledge_claims_dedup",
        "knowledge_claims",
        ["entity_key", "claim", "source_type", "user_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_index("uq_knowledge_claims_dedup", table_name="knowledge_claims")
    op.drop_index("ix_knowledge_claims_user", table_name="knowledge_claims")
    op.drop_index("ix_knowledge_claims_entity", table_name="knowledge_claims")
    op.drop_table("knowledge_claims")
    _SOURCE_TYPE.drop(op.get_bind(), checkfirst=True)
    _ENTITY_TYPE.drop(op.get_bind(), checkfirst=True)
