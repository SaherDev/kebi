"""Add `web_search` to the knowledge_source_type enum (ADR-145).

Web search gives the agent a path to facts the claims store never held, and
the findings worth keeping are written back as claims. Those claims need
their own provenance value: a search snippet is one unreviewed page, so it
carries a lower trust floor than harvested content, and marking its origin is
what lets a later sweep expire the perishable ones (schedules, prices) without
touching anything a person actually shared.

Enum-only, no data change. `ALTER TYPE ... ADD VALUE` cannot run inside a
transaction block on older PostgreSQL, so the connection is committed first —
the same shape ADR-135's enum migration used.

`downgrade` is a no-op on purpose. PostgreSQL has no `DROP VALUE`, and the
alternative (rebuild the type, rewrite the column) would destroy any claim
already written under the new value to undo a change that costs nothing to
leave in place.
"""

from alembic import op

revision = "y3z4a5b6c7d8"
down_revision = "x2y3z4a5b6c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS makes this idempotent, so a re-run (or a database that
    # already picked the value up from a create_all) is not an error.
    op.execute("COMMIT")
    op.execute("ALTER TYPE knowledge_source_type ADD VALUE IF NOT EXISTS 'web_search'")


def downgrade() -> None:
    """Intentionally empty — see the module docstring."""
