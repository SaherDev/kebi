"""add geo_key to places

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-08-19

A place's area key was derived in Python on every read, which is fine for
decorating one row and useless for the two things the Library screen needs:
grouping a user's saves by area, and fetching one area's saves without
loading the whole library. Neither can be expressed in SQL against a key the
database cannot see, and mirroring the folding rules in SQL would mean two
implementations of one rule, drifting silently (ADR-164).

So the key is stored. Nullable by design: a place whose geography is coarser
than a city has no area, which is the `elsewhere` bucket, not an error.

The backfill runs here rather than in a follow-up script, so a deploy cannot
leave the column empty — an empty column is not a visible failure, it is an
area filter that silently matches nothing. Idempotent and re-runnable by the
same code path (`scripts/rederive_geo_keys.py`), which is how a change to the
hand-maintained fold tables gets applied to rows written before it.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d9e0f1a2b3c4"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("places", sa.Column("geo_key", sa.String(), nullable=True))
    # `text_pattern_ops` so prefix matching (`geo_key LIKE 'id/bali/%'` — one
    # area including its children) uses the index regardless of collation.
    op.create_index(
        "ix_places_geo_key",
        "places",
        [sa.text("geo_key text_pattern_ops")],
        unique=False,
    )

    # Imported, never copied: the migration must backfill with exactly the
    # rule the write path applies, or the rows it writes are wrong the day
    # they land (the precedent set by the geo-claim-key migration).
    from kebi.core.areas.keys import geo_key_for_location

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, location->>'country_code' AS cc, location->>'city' AS city, "
            "location->>'neighborhood' AS hood FROM places WHERE location IS NOT NULL"
        )
    ).fetchall()

    updates = [
        {"pid": r.id, "key": key}
        for r in rows
        if (key := geo_key_for_location(r.cc, r.city, r.hood)) is not None
    ]
    for chunk_start in range(0, len(updates), 500):
        bind.execute(
            sa.text("UPDATE places SET geo_key = :key WHERE id = :pid"),
            updates[chunk_start : chunk_start + 500],
        )


def downgrade() -> None:
    op.drop_index("ix_places_geo_key", table_name="places")
    op.drop_column("places", "geo_key")
