"""add area_entities table (location-kinds Step 2)

The persistent area authority: one row per verified geographic area,
keyed in the exact `build_geo_key` format the knowledge layer already
uses (`vn`, `vn/hoi-an`) so existing knowledge_claims rows attach with
zero migration. Identity (key, name, aliases, hierarchy) is permanent;
geometry is provider content refreshed through the stored place ID
within the 30-day compliance window (`geo_refreshed_at`). `parent_key`
is a plain indexed column, no FK — a row must never fail to persist
because its parent hasn't landed yet. Pure schema addition, no
backfill: the store fills lazily read-through.

Revision ID: x2y3z4a5b6c7
Revises: w1x2y3z4a5b6
Create Date: 2026-08-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "x2y3z4a5b6c7"
down_revision: str | Sequence[str] | None = "w1x2y3z4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# create_type=False: the enum is created/dropped explicitly in
# upgrade/downgrade below (same pattern as knowledge_claims — avoids the
# duplicate CREATE TYPE that create_table would emit).
_AREA_ENTITY_TYPE = postgresql.ENUM(
    "country",
    "city",
    "neighborhood",
    name="area_entity_type",
    create_type=False,
)


def upgrade() -> None:
    _AREA_ENTITY_TYPE.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "area_entities",
        sa.Column("entity_key", sa.String(), nullable=False),
        sa.Column("entity_type", _AREA_ENTITY_TYPE, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "aliases",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lng", sa.Float(), nullable=False),
        sa.Column("bbox", postgresql.JSONB(), nullable=True),
        sa.Column("place_type", sa.String(), nullable=True),
        sa.Column("parent_key", sa.String(), nullable=True),
        sa.Column("provider_id", sa.String(), nullable=True),
        sa.Column("geo_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("entity_key"),
    )
    op.create_index("ix_area_entities_parent", "area_entities", ["parent_key"])
    op.create_index("ix_area_entities_country", "area_entities", ["country_code"])
    op.create_index(
        "ix_area_entities_aliases",
        "area_entities",
        ["aliases"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_area_entities_aliases", table_name="area_entities")
    op.drop_index("ix_area_entities_country", table_name="area_entities")
    op.drop_index("ix_area_entities_parent", table_name="area_entities")
    op.drop_table("area_entities")
    _AREA_ENTITY_TYPE.drop(op.get_bind(), checkfirst=True)
