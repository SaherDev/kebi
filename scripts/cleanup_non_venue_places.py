"""One-off cleanup: delete non-venue geography stored as venue places.

Before the validator learned to reject routes/natural features co-stamped
`tourist_attraction` (location-kinds roadmap, Step 1), non-venues like
"Ha Giang Loop" entered the catalog as venues. Raw Google types are not
persisted, so this script re-verifies each suspect row against the live
Place Details endpoint (field mask `id,types` — Essentials tier) and runs
the same `is_non_venue_geography` rule the validator now enforces.

Suspect rows are those whose `categories` is empty or exactly
`{landmark}` — the only shapes a leaked non-venue can have. Legit
landmark venues (Burj Khalifa) survive re-verification: their types
carry no geography marker.

Dry-run by default — prints the verdict table and exits. `--delete`
removes, for condemned rows only: `user_places` links, `place_embeddings`,
`interactions` referencing the place (tainted taste signals), then the
`places` rows, and finally evicts the Redis cache entries. Unresolvable
rows (HTTP error, non-google provider) are never deleted.

    poetry run python scripts/cleanup_non_venue_places.py [--limit N] [--delete]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from typing import Any, cast

import httpx
from sqlalchemy import CursorResult, text

from kebi.core.config import get_env
from kebi.core.places import RedisPlacesCache, is_non_venue_geography
from kebi.core.places._google_mapper import GOOGLE_PROVIDER_PREFIX
from kebi.db.session import _get_session_factory
from kebi.providers.redis_cache import get_redis_client

_DETAILS_URL = "https://places.googleapis.com/v1/places/{google_id}"
# Essentials-tier mask — id + types is all the verdict needs.
_FIELD_MASK = "id,types"
_CONCURRENCY = 5

_SELECT_SUSPECTS = text(
    """
    SELECT id, provider_id, place_name, categories
    FROM places
    WHERE categories = '{}' OR categories = '{landmark}'
    ORDER BY created_at
    """
)

_COUNTS = {
    "user_places": text("SELECT count(*) FROM user_places WHERE place_id = ANY(:ids)"),
    "place_embeddings": text(
        "SELECT count(*) FROM place_embeddings WHERE place_id = ANY(:ids)"
    ),
    "interactions": text(
        "SELECT count(*) FROM interactions WHERE place_id = ANY(:ids)"
    ),
}

_DELETES = [
    text("DELETE FROM user_places WHERE place_id = ANY(:ids)"),
    text("DELETE FROM place_embeddings WHERE place_id = ANY(:ids)"),
    text("DELETE FROM interactions WHERE place_id = ANY(:ids)"),
    text("DELETE FROM places WHERE id = ANY(:ids)"),
]


@dataclass
class Suspect:
    place_id: str
    provider_id: str | None
    place_name: str
    categories: list[str]
    types: list[str] | None = None
    verdict: str = "unresolvable"  # venue | non_venue | unresolvable


async def _fetch_types(http: httpx.AsyncClient, api_key: str, suspect: Suspect) -> None:
    """Fill `suspect.types` + `suspect.verdict` from live Place Details."""
    pid = suspect.provider_id
    if not pid or not pid.startswith(GOOGLE_PROVIDER_PREFIX):
        return  # non-google identity — cannot re-verify, never delete
    google_id = pid[len(GOOGLE_PROVIDER_PREFIX) :]
    try:
        response = await http.get(
            _DETAILS_URL.format(google_id=google_id),
            headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": _FIELD_MASK},
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"  ! {suspect.place_name}: details fetch failed ({exc})")
        return
    types = response.json().get("types") or []
    suspect.types = types
    suspect.verdict = "non_venue" if is_non_venue_geography(types) else "venue"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--delete",
        action="store_true",
        help="actually delete condemned rows (default: dry-run report only)",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="only examine the first N suspects"
    )
    args = parser.parse_args()

    env = get_env()
    api_key = env.GOOGLE_API_KEY
    if not api_key:
        print("GOOGLE_API_KEY is not set — cannot re-verify suspects.")
        return 1
    session_factory = _get_session_factory()

    async with session_factory() as session:
        rows = (await session.execute(_SELECT_SUSPECTS)).all()
    suspects = [
        Suspect(
            place_id=r[0],
            provider_id=r[1],
            place_name=r[2],
            categories=list(r[3] or []),
        )
        for r in rows
    ]
    if args.limit is not None:
        suspects = suspects[: args.limit]
    print(f"{len(suspects)} suspect row(s) (categories empty or landmark-only)\n")
    if not suspects:
        return 0

    sem = asyncio.Semaphore(_CONCURRENCY)

    async with httpx.AsyncClient() as http:

        async def _bounded(s: Suspect) -> None:
            async with sem:
                await _fetch_types(http, api_key, s)

        await asyncio.gather(*[_bounded(s) for s in suspects])

    condemned = [s for s in suspects if s.verdict == "non_venue"]
    kept = [s for s in suspects if s.verdict == "venue"]
    unresolvable = [s for s in suspects if s.verdict == "unresolvable"]

    for group, label in ((condemned, "NON-VENUE (delete)"), (kept, "venue (keep)")):
        print(f"== {label}: {len(group)} ==")
        for s in group:
            print(
                f"  {s.place_name!r}  categories={s.categories}  "
                f"types={s.types}  id={s.place_id}"
            )
        print()
    if unresolvable:
        print(f"== unresolvable (never deleted): {len(unresolvable)} ==")
        for s in unresolvable:
            print(f"  {s.place_name!r}  provider_id={s.provider_id}")
        print()

    if not condemned:
        print("Nothing to delete.")
        return 0

    ids = [s.place_id for s in condemned]
    async with session_factory() as session:
        for table, stmt in _COUNTS.items():
            count = (await session.execute(stmt, {"ids": ids})).scalar()
            print(f"referencing rows in {table}: {count}")

    if not args.delete:
        print("\nDry-run: nothing deleted. Re-run with --delete to remove.")
        return 0

    async with session_factory() as session:
        for stmt in _DELETES:
            result = cast(CursorResult[Any], await session.execute(stmt, {"ids": ids}))
            print(f"deleted {result.rowcount} row(s): {stmt.text.split(' WHERE')[0]}")
        await session.commit()

    provider_ids = [s.provider_id for s in condemned if s.provider_id]
    if provider_ids:
        cache = RedisPlacesCache(get_redis_client(env.REDIS_URL))
        await cache.delete_many(provider_ids)
        print(f"evicted {len(provider_ids)} cache entr(ies)")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
