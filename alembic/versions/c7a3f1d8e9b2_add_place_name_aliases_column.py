"""add_place_name_aliases_column

Adds the `place_name_aliases` JSONB column to `places_v2`. The column was
declared on `PlaceCore` and referenced by the FTS migration
(e9f0a1b2c3d4) but never created — `alembic upgrade head` failed on
"column 'place_name_aliases' does not exist". This migration slots
between the table-create and the FTS column to close the gap.

Default `'[]'::jsonb` so existing rows get an empty list and the FTS
generated column reads a valid JSONB array. NOT NULL because the model
guarantees a list (defaults to []).

Revision ID: c7a3f1d8e9b2
Revises: d1e2f3a4b5c6
Create Date: 2026-05-03 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7a3f1d8e9b2"
down_revision: str | tuple[str, ...] | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "places_v2",
        sa.Column(
            "place_name_aliases",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("places_v2", "place_name_aliases")
