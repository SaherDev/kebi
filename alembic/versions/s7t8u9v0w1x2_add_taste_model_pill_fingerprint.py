"""add taste_model.pill_fingerprint (ADR-115)

Library pills (visited/liked/approved) now train taste as a snapshot overlay,
but toggling one writes no interaction row — so the raw log_count stale-guard
can't see the change. This column stores a digest of the user's pill snapshot
at the last regen; the guard re-aggregates when it differs. Nullable, no
back-fill: a NULL never matches a computed digest, so the first post-migration
regen for each user always runs once, then stabilises. Pure schema change,
fully reversible.

Revision ID: s7t8u9v0w1x2
Revises: r6s7t8u9v0w1
Create Date: 2026-07-05 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "s7t8u9v0w1x2"
down_revision: str | Sequence[str] | None = "r6s7t8u9v0w1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "taste_model",
        sa.Column("pill_fingerprint", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("taste_model", "pill_fingerprint")
