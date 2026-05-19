"""add user_places.source_label (ADR-081)

Per-save column recording the name a place was shown as in the source
post (e.g. a TikTok card label "Mirror Temple") when it differs from
the canonical provider name, so the product can show the user the
name they know it by. Nullable; no data back-fill of existing rows
(mirrors the ADR-073/074 no-back-fill stance). Pure schema change,
fully reversible.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-05-19 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_places",
        sa.Column("source_label", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_places", "source_label")
