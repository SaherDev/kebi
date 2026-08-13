"""Re-resolve stored place geo through the fixed component parser (ADR-163).

Rows written before the ranked component fallback (ADR-119) carry only
{lat,lng,address,country} and are invisible on every area screen; rows
written before the deep-admin-level ranking store administrative names
("Kabupaten Badung") where a human area name ("Canggu") exists. The merge
kept both cohorts sticky forever, so neither self-heals.

This script re-fetches every Google-identified row with a location through
the by-id details refresh (same Essentials field mask — no billing change,
ADR-118) and replaces the stored location with the fixed parser's output,
then drops the rows' provider-cache entries so reads stop overlaying the
stale blobs.

Usage:
    poetry run python scripts/backfill_place_geo.py           # dry-run report
    poetry run python scripts/backfill_place_geo.py --apply   # write changes
    ... --missing-only   # only rows still lacking country_code/city

Dry-run still calls the provider (the new mapping IS the fetch result) but
writes nothing. Rows whose Google id was remapped are retried one-by-one —
a 1:1 call pairs its response to its row regardless of the returned id.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime

from sqlalchemy import text

from kebi.core.config import get_env
from kebi.core.places import GooglePlacesClient, RedisPlacesCache
from kebi.db.session import _get_session_factory
from kebi.providers.http_client import get_shared_http_client
from kebi.providers.redis_cache import get_redis_client

_BATCH = 50

_SELECT = (
    "SELECT id, provider_id, place_name, location FROM places "
    "WHERE provider_id LIKE 'google:%' AND location IS NOT NULL "
)
_MISSING_ONLY = "AND (NOT (location ? 'country_code') OR NOT (location ? 'city')) "

_UPDATE = text(
    "UPDATE places SET location = :location, refreshed_at = :refreshed_at "
    "WHERE id = :id"
)


def _geo(loc: dict | None) -> tuple[str | None, str | None, str | None]:
    loc = loc or {}
    return (loc.get("country_code"), loc.get("city"), loc.get("neighborhood"))


async def main(apply: bool, missing_only: bool) -> None:
    env = get_env()
    client = GooglePlacesClient(
        api_key=env.GOOGLE_API_KEY or "", http=get_shared_http_client()
    )
    cache = RedisPlacesCache(redis=get_redis_client(env.REDIS_URL))
    session_factory = _get_session_factory()

    select = _SELECT + (_MISSING_ONLY if missing_only else "") + "ORDER BY created_at"
    async with session_factory() as session:
        rows = (await session.execute(text(select))).all()
    print(f"{len(rows)} Google-identified rows with a location")

    changed = 0
    unresolved: list[str] = []
    now = datetime.now(UTC)

    for start in range(0, len(rows), _BATCH):
        batch = rows[start : start + _BATCH]
        by_pid = {row.provider_id: row for row in batch}
        fetched = {
            p.provider_id: p
            for p in await client.get_by_ids(list(by_pid))
            if p.provider_id
        }
        # A batch response with a remapped canonical id can't be paired back
        # to its row — retry those one at a time, where the pairing is
        # positional and the returned id doesn't matter.
        for pid in [p for p in by_pid if p not in fetched]:
            single = await client.get_by_ids([pid])
            if single:
                fetched[pid] = single[0]
            else:
                unresolved.append(pid)

        updates: list[dict[str, object]] = []
        purge: list[str] = []
        for pid, place in fetched.items():
            row = by_pid.get(pid)
            if row is None:
                continue
            old = row.location if isinstance(row.location, dict) else {}
            new = (
                place.location.model_dump(exclude_none=True) if place.location else None
            )
            if new is None or _geo(old) == _geo(new):
                continue
            changed += 1
            print(
                f"  {row.place_name[:40]!r}: "
                f"{'/'.join(str(p) for p in _geo(old))}"
                f" -> {'/'.join(str(p) for p in _geo(new))}"
            )
            updates.append(
                {"id": row.id, "location": json.dumps(new), "refreshed_at": now}
            )
            purge.append(pid)

        if apply and updates:
            async with session_factory() as session:
                for params in updates:
                    await session.execute(_UPDATE, params)
                await session.commit()
            await cache.delete_many(purge)

    print(
        f"\n{changed} rows {'updated' if apply else 'would change (dry-run)'}; "
        f"{len(unresolved)} provider ids did not resolve"
    )
    for pid in unresolved:
        print(f"  unresolved: {pid}")


if __name__ == "__main__":
    asyncio.run(
        main(
            apply="--apply" in sys.argv,
            missing_only="--missing-only" in sys.argv,
        )
    )
