"""drop the v1 places/embeddings store and the dormant recommendations table (ADR-078)

The places_v2 catalog is the place store of record (ADR-070/071/074) and the
taste model reads it (ADR-077). The v1 `places`/`embeddings` tables have had
no writer since the v2 cutover; the `recommendations` table (kept dormant by
ADR-075/076) has no writer and its only reader — a signal-path existence
check — was removed in this change. None of the three is referenced by any
live code path or by a places_v2 foreign key.

Note on downgrade: schema is recreated at the final pre-drop shape so the
migration is schema-reversible, but data is NOT restored and the `places`
`search_vector` generated column + `places_fts_idx` GIN index are NOT
recreated (a generated column cannot be faithfully round-tripped here).
This matches the d4e5f6a7b8c9 / c7d8e9f0a1b2 forward-only precedent.

Revision ID: f1a2b3c4d5e6
Revises: c7d8e9f0a1b2
Create Date: 2026-05-19 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "c7d8e9f0a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # FK order: embeddings.place_id -> places.id (ON DELETE CASCADE), so
    # drop the child first. recommendations is independent. DROP TABLE
    # removes each table's own indexes/constraints (incl. the places
    # generated search_vector column + places_fts_idx GIN index).
    op.drop_table("embeddings")
    op.drop_table("places")
    op.drop_table("recommendations")


def downgrade() -> None:
    # Schema-only restore at the final pre-drop shape. Data is not restored;
    # the places search_vector generated column + places_fts_idx GIN index
    # are intentionally not recreated (see module docstring).
    op.create_table(
        "places",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("place_name", sa.String(), nullable=False),
        sa.Column("place_type", sa.String(), nullable=False),
        sa.Column("subcategory", sa.String(), nullable=True),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("provider_id", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_places_user_id", "places", ["user_id"], unique=False)
    op.create_index(
        "ix_places_user_type", "places", ["user_id", "place_type"], unique=False
    )
    op.create_index(
        "uq_places_provider_id",
        "places",
        ["provider_id"],
        unique=True,
        postgresql_where=sa.text("provider_id IS NOT NULL"),
    )

    op.create_table(
        "embeddings",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("place_id", sa.String(), nullable=False),
        sa.Column("vector", Vector(1024), nullable=False),
        sa.Column("model_name", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["place_id"], ["places.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("place_id", name="uq_embeddings_place_id"),
    )

    op.create_table(
        "recommendations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_recommendations_user_id", "recommendations", ["user_id"], unique=False
    )
