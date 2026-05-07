"""add_places_v2_and_user_places_tables

Creates the places_v2 table (shared PlaceCore data, no user_id) and the
user_places table (per-user place records). Also merges the three existing
migration heads (a2b3c4d5e6f7, b3c4d5e6f7a8, a7c3d2e9f4b1) into one.

Revision ID: d1e2f3a4b5c6
Revises: a2b3c4d5e6f7, b3c4d5e6f7a8, a7c3d2e9f4b1
Create Date: 2026-04-26 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1e2f3a4b5c6"
down_revision: str | tuple[str, ...] | None = (
    "a2b3c4d5e6f7",
    "b3c4d5e6f7a8",
    "a7c3d2e9f4b1",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Geo extensions (idempotent — safe if already present)
    # ------------------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS cube")
    op.execute("CREATE EXTENSION IF NOT EXISTS earthdistance")

    # ------------------------------------------------------------------
    # places_v2 table
    # Shared across all users. Curated fields survive forever;
    # locational fields (lat, lng, address) are wiped by nightly cron
    # after 30 days (Google ToS) and refreshed inline by PlacesSearchService.
    # ------------------------------------------------------------------
    op.create_table(
        "places_v2",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("provider_id", sa.String, nullable=True),
        sa.Column("place_name", sa.String, nullable=False),
        sa.Column("category", sa.String, nullable=True),
        sa.Column("tags", sa.dialects.postgresql.JSONB, nullable=True),
        sa.Column("location", sa.dialects.postgresql.JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Partial unique index on provider_id (NULL values allowed, one per non-NULL)
    op.create_index(
        "uq_places_v2_provider_id",
        "places_v2",
        ["provider_id"],
        unique=True,
        postgresql_where=sa.text("provider_id IS NOT NULL"),
    )

    # Geo GiST index via cube+earthdistance. Extracts lat/lng from location JSONB.
    op.create_index(
        "places_v2_geo_idx",
        "places_v2",
        [sa.text("ll_to_earth((location->>'lat')::float8, (location->>'lng')::float8)")],
        postgresql_using="gist",
        postgresql_where=sa.text(
            "location IS NOT NULL"
            " AND location->>'lat' IS NOT NULL"
            " AND location->>'lng' IS NOT NULL"
        ),
    )

    # ------------------------------------------------------------------
    # user_places table
    # Per (user, place) pair. Status flags and provenance live here.
    # ------------------------------------------------------------------
    op.create_table(
        "user_places",
        sa.Column("user_place_id", sa.String, primary_key=True),
        sa.Column("user_id", sa.String, nullable=False),
        sa.Column(
            "place_id",
            sa.String,
            sa.ForeignKey("places_v2.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "approved",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "visited",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("liked", sa.Boolean, nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("source_url", sa.Text, nullable=True),
        sa.Column("saved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("visited_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index("ix_user_places_user_id", "user_places", ["user_id"])
    op.create_index("ix_user_places_place_id", "user_places", ["place_id"])
    op.create_index(
        "ix_user_places_user_saved",
        "user_places",
        ["user_id", "saved_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_places_user_saved", table_name="user_places")
    op.drop_index("ix_user_places_place_id", table_name="user_places")
    op.drop_index("ix_user_places_user_id", table_name="user_places")
    op.drop_table("user_places")

    op.drop_index("places_v2_geo_idx", table_name="places_v2")
    op.drop_index("uq_places_v2_provider_id", table_name="places_v2")
    op.drop_table("places_v2")
