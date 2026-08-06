"""canonicalize geo claim keys

Revision ID: x2y3z4a5b6c7
Revises: w1x2y3z4a5b6
Create Date: 2026-08-06

Geo claim keys are built by slugifying whatever city name the geocoder
returned, and it does not return the same one every time — an English exonym
on one lookup, the local endonym on the next. `_slugify` cannot merge those:
"Bangkok" and "Krung Thep Maha Nakhon" are different words, not
transliterations, so the slugs differ correctly and the claims split across
two keys no prefix scan will ever join (ADR-144).

`build_geo_key` now folds known variants to one canonical slug. This rewrites
the rows written before that, so existing claims move under the same key new
ones will land on.

Deliberately data-only and idempotent: it rewrites the city component of
`entity_key` for geo rows whose slug appears in the alias table, and leaves
place keys (`place:<uuid>`) untouched. Rows whose city has no known alias are
already canonical and are not touched.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "x2y3z4a5b6c7"
down_revision = "w1x2y3z4a5b6"
branch_labels = None
depends_on = None


def _aliases() -> dict[tuple[str, str], str]:
    """Read the alias table from the code that owns it.

    Imported rather than duplicated so the migration cannot drift from the
    rule it is backfilling — a copied table would silently diverge the moment
    a new pair is added.
    """
    from kebi.core.knowledge.schemas import _CITY_ALIASES

    return dict(_CITY_ALIASES)


def upgrade() -> None:
    conn = op.get_bind()
    for (country_code, variant), canonical in _aliases().items():
        old_city_key = f"{country_code}/{variant}"
        new_city_key = f"{country_code}/{canonical}"
        # The city row itself, plus everything beneath it.
        conn.execute(
            sa.text(
                "UPDATE knowledge_claims "
                "SET entity_key = :new || substr(entity_key, :cut) "
                "WHERE entity_key = :old OR entity_key LIKE :old_prefix"
            ),
            {
                "new": new_city_key,
                "cut": len(old_city_key) + 1,
                "old": old_city_key,
                "old_prefix": f"{old_city_key}/%",
            },
        )


def downgrade() -> None:
    """No-op by design.

    The rewrite is lossy: once two spellings of one city share a key, nothing
    records which row arrived under which spelling, and inventing a split
    would be worse than leaving the claims correctly merged.
    """
