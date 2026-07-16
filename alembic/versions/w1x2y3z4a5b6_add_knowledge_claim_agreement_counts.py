"""add agreement counts to knowledge_claims

Two additive counters — `agree_count` and `disagree_count` — record how many
users have corroborated or disputed a claim. Both land at 0 and stay there
until the vote write-path ships; they are surfaced now so the Library insider
note already carries the tally (and the claim `id` alongside it) for the client
to render. Non-null with a `0` server default, so existing rows backfill to 0
and behaviour is unchanged. Pure additive schema change, fully reversible.

Revision ID: w1x2y3z4a5b6
Revises: v0w1x2y3z4a5
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "w1x2y3z4a5b6"
down_revision: str | Sequence[str] | None = "v0w1x2y3z4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_claims",
        sa.Column(
            "agree_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "knowledge_claims",
        sa.Column(
            "disagree_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("knowledge_claims", "disagree_count")
    op.drop_column("knowledge_claims", "agree_count")
