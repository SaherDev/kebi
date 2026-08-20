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

# ---------------------------------------------------------------------------
# Frozen ADR-163-era derivation (see note in upgrade()).
# ---------------------------------------------------------------------------

_LEADING_ADMIN_UNITS = frozenset(
    {
        "khet",
        "khwaeng",
        "tambon",
        "amphoe",
        "chang-wat",
        "changwat",
        "kecamatan",
        "kec",
        "kabupaten",
        "kab",
        "kelurahan",
        "thanh-pho",
        "quan",
        "huyen",
        "phuong",
        "thi-xa",
    }
)
_TRAILING_ADMIN_UNITS = frozenset({"district", "regency", "subdistrict"})
_TRAILING_STRIP_COUNTRIES = frozenset({"id", "th", "vn", "la", "kh", "mm"})
_AREA_ALIASES: dict[tuple[str, str], str] = {
    ("id", "tibubeneng"): "canggu",
    ("id", "pecatu"): "uluwatu",
    ("id", "gili-indah"): "gili-trawangan",
    ("be", "antwerpen"): "antwerp",
}
_CITY_ALIASES: dict[tuple[str, str], str] = {
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


def _snapshot_slugify(part: str) -> str:
    from anyascii import anyascii

    out: list[str] = []
    prev_hyphen = False
    for ch in anyascii(part).lower():
        if ch.isalnum():
            out.append(ch)
            prev_hyphen = False
        elif not prev_hyphen:
            out.append("-")
            prev_hyphen = True
    return "".join(out).strip("-")


def _snapshot_area_slug(slug: str, country_code: str | None) -> str:
    for token in sorted(_LEADING_ADMIN_UNITS, key=len, reverse=True):
        prefix = f"{token}-"
        if slug.startswith(prefix) and len(slug) > len(prefix):
            slug = slug[len(prefix) :]
            break
    if (
        country_code is not None
        and country_code.strip().lower() in _TRAILING_STRIP_COUNTRIES
    ):
        for token in _TRAILING_ADMIN_UNITS:
            suffix = f"-{token}"
            if slug.endswith(suffix) and len(slug) > len(suffix):
                slug = slug.removesuffix(suffix)
                break
    if country_code is not None:
        slug = _AREA_ALIASES.get((country_code.strip().lower(), slug), slug)
    return slug


def _snapshot_city_slug(country_code: str, city: str) -> str:
    cc = country_code.strip().lower()
    slug = _snapshot_slugify(city)
    if (cc, slug) in _CITY_ALIASES:
        return _CITY_ALIASES[(cc, slug)]
    folded = _snapshot_area_slug(slug, cc)
    return _CITY_ALIASES.get((cc, folded), folded)


def _snapshot_geo_key_for_location(
    country_code: str | None, city: str | None, neighborhood: str | None
) -> str | None:
    if not country_code or not city:
        return None
    cc = country_code.strip().lower()
    if len(cc) != 2 or not cc.isalpha():
        return None
    parts = [cc, _snapshot_city_slug(cc, city)]
    if neighborhood:
        parts.append(_snapshot_area_slug(_snapshot_slugify(neighborhood), cc))
    return "/".join(parts)


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

    # Frozen snapshot of the ADR-163/165-era key derivation. This was
    # imported from `core.areas.keys` when this migration shipped; the geo
    # identity registry later deleted the slug derivation from live code, so
    # the rule this backfill actually applied is pinned here verbatim. On a
    # fresh (empty) database the backfill is a no-op either way, and the
    # identity migration script re-derives every stored key afterwards.
    geo_key_for_location = _snapshot_geo_key_for_location

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
