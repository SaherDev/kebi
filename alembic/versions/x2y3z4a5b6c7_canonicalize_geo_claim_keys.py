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
    """Frozen snapshot of the ADR-144 city alias table.

    This was imported from `core.knowledge.schemas` while that table lived;
    the geo identity registry later deleted it from live code, so the pairs
    this migration actually applied are pinned here verbatim. On a fresh
    (empty) database this is a no-op either way.
    """
    return {
        ("th", "krung-thep-maha-nakhon"): "bangkok",
        ("th", "krung-thep"): "bangkok",
        ("id", "jakarta-raya"): "jakarta",
        ("id", "daerah-khusus-ibukota-jakarta"): "jakarta",
        ("id", "nusa-tenggara-barat"): "west-nusa-tenggara",
        ("id", "nusa-tenggara-timur"): "east-nusa-tenggara",
        ("vn", "thanh-pho-ho-chi-minh"): "ho-chi-minh-city",
        ("vn", "sai-gon"): "ho-chi-minh-city",
        ("vn", "ha-noi"): "hanoi",
        ("vn", "da-nang"): "da-nang",
        ("jp", "tokyo-to"): "tokyo",
        ("kr", "seoul-teukbyeolsi"): "seoul",
        ("cn", "beijing-shi"): "beijing",
        ("ae", "dubayy"): "dubai",
    }


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
