"""add review state to knowledge_claims (ADR-122)

Prepares the claims store for a future approval step (an AI reviewer or the
team) before a claim goes live. Three columns land now, unused by any gate
yet: `review_status` (pending | approved | rejected), and `reviewed_by` /
`reviewed_at` recording who/what set the current status and when. The default
is `approved` — today the product trusts every writer, so behaviour is
unchanged and existing rows backfill to `approved`; when review turns on, a
writer's default status becomes config, not code. `reviewed_by`/`reviewed_at`
stay NULL until an actual review happens (an auto-trusted claim was never
reviewed). Pure additive schema change, fully reversible.

Revision ID: v0w1x2y3z4a5
Revises: u9v0w1x2y3z4
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "v0w1x2y3z4a5"
down_revision: str | Sequence[str] | None = "u9v0w1x2y3z4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REVIEW_STATUS = postgresql.ENUM(
    "pending",
    "approved",
    "rejected",
    name="knowledge_review_status",
    # Created/dropped explicitly below; keep add_column from re-emitting
    # CREATE TYPE (see the note in the ADR-120 migration).
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    _REVIEW_STATUS.create(bind, checkfirst=True)

    op.add_column(
        "knowledge_claims",
        sa.Column(
            "review_status",
            _REVIEW_STATUS,
            nullable=False,
            server_default="approved",
        ),
    )
    op.add_column(
        "knowledge_claims",
        sa.Column("reviewed_by", sa.String(), nullable=True),
    )
    op.add_column(
        "knowledge_claims",
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_knowledge_claims_review_status",
        "knowledge_claims",
        ["review_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_claims_review_status", table_name="knowledge_claims")
    op.drop_column("knowledge_claims", "reviewed_at")
    op.drop_column("knowledge_claims", "reviewed_by")
    op.drop_column("knowledge_claims", "review_status")
    _REVIEW_STATUS.drop(op.get_bind(), checkfirst=True)
