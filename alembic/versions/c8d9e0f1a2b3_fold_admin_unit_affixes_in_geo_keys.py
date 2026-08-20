"""fold admin-unit affixes in geo keys

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-08-13

Catalog rows carry the provider's administrative geo, so a save in Canggu
stored its regency and keyed `id/bali/kabupaten-badung` while the chat path
keyed the same area `id/bali/canggu` — one area split across two keys, the
neighborhood-level variant of the city split ADR-144 repaired (flagged as
the standing gap in ADR-153, closed by ADR-163).

`build_geo_key` now folds admin-unit affixes ("Khet", "Kabupaten",
"Thành phố", trailing "District"/"Regency") out of both the city and
neighborhood components. This moves the rows written before that: claim
keys, area rows (key, display name, and recorded child keys) — so existing
knowledge lands under the same key new writes will.

Deliberately data-only and idempotent: a key that folds to itself is not
touched, and an area row whose folded key already exists is deleted as the
admin-named duplicate of the row that keeps the key (the profiler recreates
any area on first open, so no content is lost that cannot regenerate).
"""

from __future__ import annotations

import json
import re

import sqlalchemy as sa

from alembic import op

revision = "c8d9e0f1a2b3"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None

_GEO_KEY_RE = re.compile(r"^[a-z]{2}(/.+)?$")

# ---------------------------------------------------------------------------
# Frozen snapshot of the ADR-163-era fold logic. This lived in
# `core.knowledge.schemas` when this migration shipped and was imported from
# there; the geo identity registry later deleted it from live code, so the
# rule is pinned here verbatim. Data migrations only ever re-run against
# fresh (empty) databases, where these are no-ops — the snapshot exists so
# `alembic upgrade head` keeps importing cleanly forever.
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


def _strips_trailing(country_code: str | None) -> bool:
    return (
        country_code is not None
        and country_code.strip().lower() in _TRAILING_STRIP_COUNTRIES
    )


def canonical_area_slug(slug: str, country_code: str | None = None) -> str:
    for token in sorted(_LEADING_ADMIN_UNITS, key=len, reverse=True):
        prefix = f"{token}-"
        if slug.startswith(prefix) and len(slug) > len(prefix):
            slug = slug[len(prefix) :]
            break
    if _strips_trailing(country_code):
        for token in _TRAILING_ADMIN_UNITS:
            suffix = f"-{token}"
            if slug.endswith(suffix) and len(slug) > len(suffix):
                slug = slug.removesuffix(suffix)
                break
    if country_code is not None:
        slug = _AREA_ALIASES.get((country_code.strip().lower(), slug), slug)
    return slug


def canonical_city_slug(country_code: str, city: str) -> str:
    cc = country_code.strip().lower()
    slug = _snapshot_slugify(city)
    if (cc, slug) in _CITY_ALIASES:
        return _CITY_ALIASES[(cc, slug)]
    folded = canonical_area_slug(slug, cc)
    return _CITY_ALIASES.get((cc, folded), folded)


def strip_admin_unit(name: str, country_code: str | None = None) -> str:
    words = name.split()
    stripped = list(words)
    for n in (2, 1):
        if len(stripped) > n and _snapshot_slugify(" ".join(stripped[:n])) in (
            _LEADING_ADMIN_UNITS
        ):
            stripped = stripped[n:]
            break
    if (
        _strips_trailing(country_code)
        and len(stripped) > 1
        and _snapshot_slugify(stripped[-1]) in _TRAILING_ADMIN_UNITS
    ):
        stripped = stripped[:-1]
    return " ".join(stripped) if stripped != words else name


def _fold_key(key: str) -> str | None:
    """The canonical form of a geo key under ADR-163, or None if unchanged."""
    if not _GEO_KEY_RE.match(key):
        return None
    parts = key.split("/")
    if len(parts) < 2:
        return None
    folded = [parts[0], canonical_city_slug(parts[0], parts[1])]
    if len(parts) > 2:
        folded.append(canonical_area_slug(parts[2], parts[0]))
        folded.extend(parts[3:])
    new_key = "/".join(folded)
    return new_key if new_key != key else None


def upgrade() -> None:
    conn = op.get_bind()

    # -- knowledge_claims: rewrite folding entity keys wholesale ------------
    claim_keys = conn.execute(
        sa.text("SELECT DISTINCT entity_key FROM knowledge_claims")
    ).scalars()
    for key in claim_keys:
        new_key = _fold_key(key)
        if new_key is None:
            continue
        conn.execute(
            sa.text(
                "UPDATE knowledge_claims SET entity_key = :new WHERE entity_key = :old"
            ),
            {"new": new_key, "old": key},
        )

    # -- areas: fold row keys, display names, and recorded child keys -------
    rows = conn.execute(
        sa.text("SELECT geo_key, name, notable_sub_areas FROM areas")
    ).all()
    existing_keys = {row.geo_key for row in rows}
    for row in rows:
        new_key = _fold_key(row.geo_key)
        if new_key is not None and new_key in existing_keys:
            # The folded key already has its own (colloquially-named) row —
            # this row is the admin-named duplicate. Regenerable on demand.
            conn.execute(
                sa.text("DELETE FROM areas WHERE geo_key = :old"),
                {"old": row.geo_key},
            )
            continue

        updates: dict[str, object] = {}
        if new_key is not None:
            updates["geo_key"] = new_key
            existing_keys.add(new_key)
            stripped_name = strip_admin_unit(row.name, row.geo_key.split("/")[0])
            if stripped_name != row.name:
                updates["name"] = stripped_name

        children = row.notable_sub_areas
        if isinstance(children, str):
            children = json.loads(children)
        folded_children = []
        children_changed = False
        for child in children or []:
            child_key = child.get("geo_key")
            folded_child = _fold_key(child_key) if child_key else None
            if folded_child is not None:
                child = {**child, "geo_key": folded_child}
                children_changed = True
            folded_children.append(child)
        if children_changed:
            updates["notable_sub_areas"] = json.dumps(folded_children)

        if not updates:
            continue
        set_clause = ", ".join(
            f"{col} = :{col}" + ("::jsonb" if col == "notable_sub_areas" else "")
            for col in updates
        )
        conn.execute(
            sa.text(f"UPDATE areas SET {set_clause} WHERE geo_key = :old"),  # noqa: S608
            {**updates, "old": row.geo_key},
        )


def downgrade() -> None:
    """No-op by design.

    The fold is lossy: once the admin-styled and human-named spellings share
    a key, nothing records which rows arrived under which spelling, and
    inventing a split would be worse than leaving them correctly merged.
    """
