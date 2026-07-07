"""rename user_places.source_url to source_ref

Revision ID: e1f2a3b4c5d6
Revises: c3d4e5f6a7b8
Create Date: 2026-05-25

The column's semantic is "opaque pointer to the place's origin," not
specifically a URL — it holds the canonical URL for tiktok/instagram/
youtube/google_maps_list and NULL for manual/kebi. Rename to match.

Forward and back are pure ALTER COLUMN renames; no data migration needed.
"""

from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("user_places", "source_url", new_column_name="source_ref")


def downgrade() -> None:
    op.alter_column("user_places", "source_ref", new_column_name="source_url")
