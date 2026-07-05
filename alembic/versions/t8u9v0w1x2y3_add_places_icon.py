"""add places.icon (ADR-117)

Per-place display icon — a single emoji picked by an LLM at the point one
already sees the place (extraction classification, consult naming). Nullable
by design: LLM-less paths (provider-driven discovery, raw Google
write-through) leave it NULL and the client falls back to its category map.
Fill-only semantics live in the merge policy, not the schema. Pure schema
change, fully reversible.

Revision ID: t8u9v0w1x2y3
Revises: s7t8u9v0w1x2
Create Date: 2026-07-05 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "t8u9v0w1x2y3"
down_revision: str | Sequence[str] | None = "s7t8u9v0w1x2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "places",
        sa.Column("icon", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("places", "icon")
