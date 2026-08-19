"""Re-resolve every stored place's area from its coordinates.

New writes get this from the upsert gateway. Rows written before it kept the
area their Place Details response happened to name, and that is the data
that disagrees with itself — the same island addressed two ways, a province
in the local language on one row and English on the next.

Reads coordinates, asks the geocoder what contains them, and fills in only
the fields a row is missing. It never renames: the geocoder answers with the
administrative village ("Tibubeneng" for a place everyone calls Canggu), so
adopting its names wholesale is a regression, not a fix. What it does buy is
a row that had no area at all — those are the saves stranded off every area
screen.

    poetry run python scripts/backfill_area_geocode.py           # dry run
    poetry run python scripts/backfill_area_geocode.py --apply
"""

from __future__ import annotations

import asyncio
import json
import sys

from sqlalchemy import text

from kebi.core.config import get_env
from kebi.core.places.area_geocoder import GoogleAreaGeocoder
from kebi.db.session import _get_session_factory
from kebi.providers.http_client import get_shared_http_client
from kebi.providers.redis_cache import RedisCacheBackend, get_redis_client

_CONCURRENCY = 5


async def _main() -> None:
    apply = "--apply" in sys.argv
    env = get_env()
    geocoder = GoogleAreaGeocoder(
        api_key=env.GOOGLE_API_KEY or "",
        http=get_shared_http_client(),
        cache=RedisCacheBackend(client=get_redis_client(env.REDIS_URL)),
    )

    async with _get_session_factory()() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, place_name, location FROM places "
                    "WHERE location->>'lat' IS NOT NULL"
                )
            )
        ).fetchall()
        print(f"{len(rows)} rows with coordinates")

        sem = asyncio.Semaphore(_CONCURRENCY)

        async def _one(row: object) -> tuple[str, dict[str, object]] | None:
            loc = row.location  # type: ignore[attr-defined]
            loc = json.loads(loc) if isinstance(loc, str) else dict(loc)
            async with sem:
                area = await geocoder.resolve(float(loc["lat"]), float(loc["lng"]))
            if area is None:
                return None
            # Fills gaps only, never renames: the geocoder answers with the
            # administrative village, so overwriting would turn Canggu into
            # Tibubeneng and Seminyak into Kerobokan Kelod.
            updated = dict(loc)
            for field, value in (
                ("country_code", area.country_code),
                ("city", area.city),
                ("neighborhood", area.neighborhood),
            ):
                if value and not updated.get(field):
                    updated[field] = value
            if updated == loc:
                return None
            print(
                f"  {row.place_name!r}: "  # type: ignore[attr-defined]
                f"{loc.get('city')}/{loc.get('neighborhood')} -> "
                f"{updated.get('city')}/{updated.get('neighborhood')}"
            )
            return str(row.id), updated  # type: ignore[attr-defined]

        results = [r for r in await asyncio.gather(*[_one(r) for r in rows]) if r]

        if apply and results:
            for place_id, loc in results:
                await session.execute(
                    text("UPDATE places SET location = :loc WHERE id = :pid"),
                    {"loc": json.dumps(loc), "pid": place_id},
                )
            await session.commit()

        print(f"\n{len(results)} rows {'updated' if apply else 'would change'}")
        if results and not apply:
            print("re-run with --apply, then scripts/rederive_geo_keys.py")


if __name__ == "__main__":
    asyncio.run(_main())
