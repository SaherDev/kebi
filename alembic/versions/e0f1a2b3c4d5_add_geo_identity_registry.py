"""add geo identity registry

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
Create Date: 2026-08-20

Geo identity was derived from display strings and patched by hand-maintained
fold tables that missed silently everywhere the team hadn't personally been.
The registry replaces that: one row per geographic unit ever seen, keyed by
the provider's stable place id, minted lazily by one geocoder lookup and then
joined by alias forever. Keys become id-paths; every name a user sees is
registry data.

Schema only, deliberately: minting rows for existing data needs the network,
and a migration that can hang on an external call is how ADR-166 happened.
The data move (mint rows for every stored key, rewrite places.geo_key,
knowledge_claims.entity_key, and areas.geo_key, record legacy keys) lives in
`scripts/migrate_geo_identity.py`, run explicitly after deploy.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "e0f1a2b3c4d5"
down_revision = "d9e0f1a2b3c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "geo_areas",
        sa.Column("place_id", sa.String(), primary_key=True),
        sa.Column("provider", sa.String(), nullable=False, server_default="google"),
        sa.Column("country_code", sa.String(), nullable=False),
        sa.Column("slot", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("colloquial_name", sa.String(), nullable=True),
        sa.Column("groups_into", sa.String(), nullable=True),
        sa.Column("split_of", sa.String(), nullable=True),
        sa.Column("city_place_id", sa.String(), nullable=True),
        sa.Column("geo_key", sa.String(), nullable=False),
        sa.Column("legacy_key", sa.String(), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lng", sa.Float(), nullable=True),
        sa.Column("viewport", JSONB(), nullable=True),
        sa.Column(
            "ambiguous", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "minted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("geo_key", name="uq_geo_areas_geo_key"),
    )
    op.create_index(
        "ix_geo_areas_legacy_key",
        "geo_areas",
        ["legacy_key"],
        postgresql_where=sa.text("legacy_key IS NOT NULL"),
    )
    op.create_index(
        "ix_geo_areas_split_of",
        "geo_areas",
        ["split_of"],
        postgresql_where=sa.text("split_of IS NOT NULL"),
    )

    op.create_table(
        "geo_area_aliases",
        sa.Column("country_code", sa.String(), primary_key=True),
        sa.Column(
            "city_place_id", sa.String(), primary_key=True, server_default=""
        ),
        sa.Column("slug", sa.String(), primary_key=True),
        sa.Column("place_id", sa.String(), nullable=False),
    )
    op.create_index(
        "ix_geo_area_aliases_place_id", "geo_area_aliases", ["place_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_geo_area_aliases_place_id", table_name="geo_area_aliases")
    op.drop_table("geo_area_aliases")
    op.drop_index("ix_geo_areas_split_of", table_name="geo_areas")
    op.drop_index("ix_geo_areas_legacy_key", table_name="geo_areas")
    op.drop_table("geo_areas")
