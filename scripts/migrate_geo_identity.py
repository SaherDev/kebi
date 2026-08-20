"""One-off data migration: slug geo keys → geo-registry id keys.

Run once after deploying the registry schema (`alembic upgrade head`). Lives
in a script, not a migration, because it talks to the network: every unique
area in the data is minted through the geo registry (one verified Google
Geocoding lookup + one cached LLM colloquial pass each), and a migration
that can hang on an external call is how ADR-166 happened.

What it does, in order:

1. Collects every distinct legacy geo key from `places.geo_key`,
   `knowledge_claims.entity_key`, and `areas.geo_key` (plus ancestors) and
   mints their registry rows, recording each old key on its row so tokens
   in old chat messages keep resolving forever.
2. Re-derives `places.geo_key` from each row's stored location through the
   registry — coordinates included, so ambiguous units (the Gili class)
   land on the right island.
3. Rewrites `knowledge_claims.entity_key` through the old→new mapping.
4. Re-keys `areas` rows (and their recorded child keys); a row whose folded
   key collides with an existing one, or that cannot map, is deleted — the
   profiler regenerates any area on first open (ADR-163 precedent).

Idempotent: keys already in the id grammar are left alone, minting is
upsert-idempotent, and re-running only touches rows whose derived value
still differs.

    poetry run python -m scripts.migrate_geo_identity          # apply
    poetry run python -m scripts.migrate_geo_identity --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from kebi.core.areas.keys import is_legacy_geo_key
from kebi.core.config import get_env
from kebi.core.geo.google_lookup import GoogleGeoLookupClient
from kebi.core.geo.registry import GeoRegistry
from kebi.db.repositories.geo_area_repository import SQLAlchemyGeoAreaRepository
from kebi.db.session import _get_session_factory
from kebi.providers import get_instructor_client

logger = logging.getLogger("migrate_geo_identity")

_CHUNK = 500


def _name_from_slug(slug: str) -> str:
    """A lookup hint for the registry: the slug's words, space-joined.

    Good enough on purpose — the registry verifies every lookup against the
    geocoder's own record, so a hint that can't verify maps to nothing
    rather than to the wrong unit.
    """
    return slug.replace("-", " ")


async def _collect_legacy_keys(session: AsyncSession) -> set[str]:
    keys: set[str] = set()
    for query in (
        "SELECT DISTINCT geo_key FROM places WHERE geo_key IS NOT NULL",
        "SELECT DISTINCT entity_key FROM knowledge_claims "
        "WHERE entity_key NOT LIKE 'place:%'",
        "SELECT geo_key FROM areas",
    ):
        for (key,) in (await session.execute(text(query))).all():
            if key and is_legacy_geo_key(key):
                keys.add(key.strip("/"))
    # Ancestors too: an old city token must map even when only its
    # neighborhoods ever held data.
    for key in list(keys):
        parts = key.split("/")
        for i in range(2, len(parts)):
            keys.add("/".join(parts[:i]))
    return keys


async def _build_mapping(
    registry: GeoRegistry,
    repo: SQLAlchemyGeoAreaRepository,
    legacy_keys: set[str],
) -> dict[str, str | None]:
    """Mint every legacy key's unit and map old → new (None = unresolvable).

    Shallow keys first so cities exist before their areas mint under them.
    A key that resolves shallower than asked maps to the resolved depth —
    coarser-but-correct, and its claims stay reachable through ancestors.
    """
    mapping: dict[str, str | None] = {}
    for old_key in sorted(legacy_keys, key=lambda k: (k.count("/"), k)):
        parts = old_key.split("/")
        cc = parts[0]
        city = _name_from_slug(parts[1]) if len(parts) > 1 else None
        hood = _name_from_slug(parts[2]) if len(parts) > 2 else None
        resolved = await registry.key_for_location(cc, city, hood, mint=True)
        if resolved is None:
            mapping[old_key] = None
            continue
        mapping[old_key] = resolved.geo_key
        leaf = resolved.leaf
        depth_matches = (len(parts) - 1) == (
            (1 if resolved.city else 0) + (1 if resolved.area else 0)
        )
        if leaf is not None and depth_matches:
            if leaf.legacy_key is None:
                await repo.set_legacy_key(leaf.place_id, old_key)
            elif leaf.legacy_key != old_key:
                # A second slug spelling for the same unit — exactly the
                # split the registry ends. Recorded as a legacy alias so
                # old tokens under either spelling resolve.
                await repo.add_alias(cc, "legacy", old_key, leaf.place_id)
    return mapping


async def _rederive_places(
    session: AsyncSession, registry: GeoRegistry, *, dry_run: bool
) -> int:
    rows = (
        await session.execute(
            text(
                "SELECT id, geo_key, "
                "location->>'country_code' AS cc, "
                "location->>'city' AS city, "
                "location->>'neighborhood' AS hood, "
                "location->>'lat' AS lat, "
                "location->>'lng' AS lng "
                "FROM places WHERE location IS NOT NULL"
            )
        )
    ).fetchall()
    changed: list[dict[str, str | None]] = []
    for r in rows:
        resolved = await registry.key_for_location(
            r.cc,
            r.city,
            r.hood,
            lat=float(r.lat) if r.lat else None,
            lng=float(r.lng) if r.lng else None,
            mint=True,
        )
        key = resolved.geo_key if resolved and resolved.city else None
        if key != r.geo_key:
            changed.append({"pid": r.id, "key": key})
    if not dry_run:
        for start in range(0, len(changed), _CHUNK):
            await session.execute(
                text("UPDATE places SET geo_key = :key WHERE id = :pid"),
                changed[start : start + _CHUNK],
            )
        await session.commit()
    return len(changed)


async def _rewrite_claims(
    session: AsyncSession, mapping: dict[str, str | None], *, dry_run: bool
) -> int:
    changed = 0
    for old_key, new_key in mapping.items():
        if new_key is None or new_key == old_key:
            continue
        if dry_run:
            count = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM knowledge_claims WHERE entity_key = :old"
                    ),
                    {"old": old_key},
                )
            ).scalar_one()
            changed += int(count)
            continue
        result = await session.execute(
            text(
                "UPDATE knowledge_claims SET entity_key = :new WHERE entity_key = :old"
            ),
            {"new": new_key, "old": old_key},
        )
        changed += result.rowcount or 0
    if not dry_run:
        await session.commit()
    return changed


async def _rekey_areas(
    session: AsyncSession, mapping: dict[str, str | None], *, dry_run: bool
) -> tuple[int, int]:
    rows = (
        await session.execute(text("SELECT geo_key, notable_sub_areas FROM areas"))
    ).all()
    existing = {row.geo_key for row in rows}
    rekeyed = deleted = 0
    for row in rows:
        if not is_legacy_geo_key(row.geo_key):
            continue
        new_key = mapping.get(row.geo_key.strip("/"))
        if new_key is None or new_key in existing:
            deleted += 1
            if not dry_run:
                await session.execute(
                    text("DELETE FROM areas WHERE geo_key = :old"),
                    {"old": row.geo_key},
                )
            continue
        children = row.notable_sub_areas
        if isinstance(children, str):
            children = json.loads(children)
        kept_children = []
        for child in children or []:
            child_new = mapping.get((child.get("geo_key") or "").strip("/"))
            if child_new is not None:
                kept_children.append({**child, "geo_key": child_new})
        rekeyed += 1
        existing.add(new_key)
        if not dry_run:
            await session.execute(
                text(
                    "UPDATE areas SET geo_key = :new, "
                    "notable_sub_areas = :children::jsonb "
                    "WHERE geo_key = :old"
                ),
                {
                    "new": new_key,
                    "children": json.dumps(kept_children),
                    "old": row.geo_key,
                },
            )
    if not dry_run:
        await session.commit()
    return rekeyed, deleted


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    async with httpx.AsyncClient() as http:
        repo = SQLAlchemyGeoAreaRepository(_get_session_factory())
        registry = GeoRegistry(
            repo=repo,
            lookup=GoogleGeoLookupClient(
                api_key=get_env().GOOGLE_API_KEY or "", http=http
            ),
            instructor_client=get_instructor_client("area_registry"),
        )
        async with _get_session_factory()() as session:
            legacy_keys = await _collect_legacy_keys(session)
            logger.info("legacy keys found: %d", len(legacy_keys))
            mapping = await _build_mapping(registry, repo, legacy_keys)
            unresolved = sorted(k for k, v in mapping.items() if v is None)
            logger.info(
                "mapped: %d, unresolvable: %d %s",
                sum(v is not None for v in mapping.values()),
                len(unresolved),
                unresolved[:20],
            )
            places_changed = await _rederive_places(
                session, registry, dry_run=args.dry_run
            )
            logger.info("places re-keyed: %d", places_changed)
            claims_changed = await _rewrite_claims(
                session, mapping, dry_run=args.dry_run
            )
            logger.info("claims re-keyed: %d", claims_changed)
            rekeyed, deleted = await _rekey_areas(
                session, mapping, dry_run=args.dry_run
            )
            logger.info("areas re-keyed: %d, deleted: %d", rekeyed, deleted)
    if args.dry_run:
        logger.info("dry run — nothing written except registry mints")


if __name__ == "__main__":
    asyncio.run(_main())
