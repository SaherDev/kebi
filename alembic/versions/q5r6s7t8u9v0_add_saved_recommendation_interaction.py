"""add_saved_recommendation_interaction

Add a `saved_recommendation` value to the `interactiontype` enum. Saving a
place kebi recommended (the consult card's "save it" action) is a stronger
positive than a passive link-share `save`, so it gets its own interaction
type with its own taste weight rather than sharing the `save` bucket.

`ALTER TYPE ... ADD VALUE` cannot run inside a transaction block, so it is
issued in an autocommit block. `IF NOT EXISTS` makes it idempotent — safe to
re-run on a DB that already has the value.

Postgres has no `DROP VALUE`, so the downgrade is a documented no-op: an added
enum label cannot be removed without rebuilding the type, which would be
data-lossy for any row already using it.

Revision ID: q5r6s7t8u9v0
Revises: p4q5r6s7t8u9
Create Date: 2026-06-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "q5r6s7t8u9v0"
down_revision: str | Sequence[str] | None = "p4q5r6s7t8u9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE interactiontype ADD VALUE IF NOT EXISTS "
            "'saved_recommendation'"
        )


def downgrade() -> None:
    # Postgres cannot drop an enum value. Removing 'saved_recommendation'
    # would require recreating interactiontype and rewriting every row — and
    # is data-lossy for rows already using it. Intentional no-op.
    pass
