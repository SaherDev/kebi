"""add user_intents table (ADR-110)

Backs the home screen's "what you wanted" recall list. Persists the user's
intent-bearing chat turns (the natural-language text, verbatim) in a store
separate from the `interactions` taste-signal log, so its row count never
perturbs the taste-regen thresholds. Composite (user_id, created_at) index
serves the newest-first keyset page.

No foreign key to users (Constitution VI: cross-repo boundary). Pure schema
change, fully reversible.

Revision ID: r6s7t8u9v0w1
Revises: q5r6s7t8u9v0
Create Date: 2026-06-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r6s7t8u9v0w1"
down_revision: str | Sequence[str] | None = "q5r6s7t8u9v0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_intents",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_intents_user_created",
        "user_intents",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_intents_user_created", table_name="user_intents")
    op.drop_table("user_intents")
