"""Recompute `places.geo_key` from each row's stored location (ADR-165).

Run this after changing the hand-maintained fold tables — `_CITY_ALIASES`,
`_AREA_ALIASES`, or the admin-unit affix lists in `core.knowledge.schemas`.
Those tables are the reason the key is *derived* rather than authored, and
the reason a stored copy needs a way back in sync: adding
`("id", "kerobokan"): "canggu"` changes which area existing saves belong to,
and rows written before it keep pointing at the old key until this runs.

Deriving on read would avoid the job entirely, but the key has to be visible
to SQL — grouping a library by area and fetching one area's saves are both
set operations the database must do, not the application. Storing it is what
buys that; this script is the maintenance that storing costs.

Idempotent, and safe to run against a live database: it only ever writes
`geo_key`, and only where the derived value differs from what is stored.

    poetry run python -m scripts.rederive_geo_keys
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from kebi.core.areas.keys import geo_key_for_location
from kebi.db.session import _get_session_factory

logger = logging.getLogger(__name__)

_CHUNK = 500


async def rederive(session: AsyncSession) -> int:
    """Recompute every place's geo_key. Returns how many rows changed.

    Reads the location components rather than whole rows — the derivation
    needs three strings, and a catalog scan should not haul JSONB blobs it
    will not read.
    """
    rows = (
        await session.execute(
            text(
                "SELECT id, geo_key, "
                "location->>'country_code' AS cc, "
                "location->>'city' AS city, "
                "location->>'neighborhood' AS hood "
                "FROM places"
            )
        )
    ).fetchall()

    changed = [
        {"pid": r.id, "key": derived}
        for r in rows
        if (derived := geo_key_for_location(r.cc, r.city, r.hood)) != r.geo_key
    ]
    for start in range(0, len(changed), _CHUNK):
        await session.execute(
            text("UPDATE places SET geo_key = :key WHERE id = :pid"),
            changed[start : start + _CHUNK],
        )
    await session.commit()
    return len(changed)


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    async with _get_session_factory()() as session:
        changed = await rederive(session)
    logger.info("re-derived geo_key on %d row(s)", changed)


if __name__ == "__main__":
    asyncio.run(_main())
