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


def _fold_key(key: str) -> str | None:
    """The canonical form of a geo key under ADR-163, or None if unchanged.

    Imported from the code that owns the rule rather than duplicated, so the
    migration cannot drift from what `build_geo_key` produces.
    """
    from kebi.core.knowledge.schemas import canonical_area_slug, canonical_city_slug

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
    from kebi.core.knowledge.schemas import strip_admin_unit

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
