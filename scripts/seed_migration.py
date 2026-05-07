"""Seed migration for feature 019 (PlacesService, ADR-054).

Run this BEFORE `alembic upgrade head` for the `places_service_schema`
revision. It relocates legacy data that the Alembic migration is about to
drop:

  * cuisine, price_range, ambiance  → attributes JSONB (in-place update)
  * lat/lng/address for rows with a provider_id  → Redis Tier 2 cache
    under `places:geo:{provider_id}`, TTL from config.places.cache_ttl_days
  * place_type backfill via a heuristic ladder:
      (1) cuisine non-null                   → food_and_drink
      (2) nature/museum keyword in name      → things_to_do
      (3) otherwise                          → services (DEFAULTED — logged)
  * subcategory: LEFT NULL for every legacy row. A blanket cuisine→restaurant
    mapping is too broad (cuisine=japanese could be cafe/bar/izakaya). The LLM
    enricher sets subcategory correctly on the next extraction.

The script is idempotent: re-running does not corrupt data (it checks for
existing non-null values before writing).

Operator review gate:
  Any row that falls through to the default `place_type='services'` is
  logged as `place_type_defaulted` with its row id and place_name. If any
  such row exists, the script exits with code 2 so the operator must
  acknowledge before proceeding to `alembic upgrade head`. To accept the
  defaults and proceed, re-run with `--accept-defaults`.

Usage:
    python scripts/seed_migration.py             # review required if defaults
    python scripts/seed_migration.py --accept-defaults

Exit codes:
    0 — success (no defaults or --accept-defaults passed)
    1 — error during execution
    2 — success but defaults present; review required
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from kebi.core.config import get_config
from kebi.core.places.models import GeoData

LOG_FILE = "scripts/seed_migration.log"

logger = logging.getLogger("seed_migration")

# ---------------------------------------------------------------------------
# place_type heuristic ladder
# ---------------------------------------------------------------------------

_THINGS_TO_DO_KEYWORDS = frozenset(
    {
        "museum",
        "park",
        "temple",
        "shrine",
        "gallery",
        "zoo",
        "aquarium",
        "palace",
        "castle",
        "monument",
        "mountain",
        "beach",
        "garden",
        "waterfall",
    }
)
_SHOPPING_KEYWORDS = frozenset(
    {
        "mall",
        "boutique",
        "market",
        "store",
        "bookstore",
        "shop",
    }
)
_ACCOMMODATION_KEYWORDS = frozenset(
    {
        "hotel",
        "hostel",
        "resort",
        "inn",
        "ryokan",
        "guesthouse",
        "rental",
    }
)
_SERVICES_KEYWORDS = frozenset(
    {
        "gym",
        "spa",
        "salon",
        "barber",
        "coworking",
        "pharmacy",
        "clinic",
        "laundry",
    }
)


def _count_hits(name_lower: str, keywords: frozenset[str]) -> int:
    return sum(1 for kw in keywords if kw in name_lower)


def infer_place_type(place_name: str | None, cuisine: str | None) -> tuple[str, bool]:
    """Return (place_type, defaulted) per the keyword ladder.

    1. Check every non-food category's keyword set in parallel. The set with
       the most hits wins. Ties prefer things_to_do → shopping →
       accommodation → services (alphabetical-ish; matches legacy behaviour).
    2. If no keyword matched, fall back to the cuisine signal → food_and_drink.
    3. Otherwise default to `services` with the existing defaulted log line.
    """
    name_lower = (place_name or "").lower()

    hits: dict[str, int] = {
        "things_to_do": _count_hits(name_lower, _THINGS_TO_DO_KEYWORDS),
        "shopping": _count_hits(name_lower, _SHOPPING_KEYWORDS),
        "accommodation": _count_hits(name_lower, _ACCOMMODATION_KEYWORDS),
        "services": _count_hits(name_lower, _SERVICES_KEYWORDS),
    }
    best = max(hits.items(), key=lambda kv: kv[1])
    if best[1] > 0:
        return best[0], False

    if cuisine is not None and cuisine.strip() != "":
        return "food_and_drink", False

    return "services", True


def map_price_hint(price_range: str | None) -> str | None:
    """Map low/mid/high → cheap/moderate/expensive. Anything else → None."""
    if price_range is None:
        return None
    mapping = {"low": "cheap", "mid": "moderate", "high": "expensive"}
    return mapping.get(price_range.lower())


# ---------------------------------------------------------------------------
# Counters for the report
# ---------------------------------------------------------------------------


@dataclass
class Counters:
    scanned: int = 0
    cuisine_relocated: int = 0
    price_mapped: int = 0
    price_unmapped: int = 0
    ambiance_relocated: int = 0
    geo_seeded: int = 0
    geo_lost_no_pid: int = 0
    place_type_food_and_drink: int = 0
    place_type_things_to_do: int = 0
    place_type_defaulted: int = 0
    defaulted_rows: list[tuple[str, str]] = field(default_factory=list)
    lost_geo_rows: list[tuple[str, str]] = field(default_factory=list)
    unmapped_prices: list[tuple[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core relocation logic (async, one session, one Redis pipeline for geo)
# ---------------------------------------------------------------------------


async def _relocate(
    session: AsyncSession,
    redis_client: aioredis.Redis,
    counters: Counters,
) -> None:
    """Walk every legacy row, relocate data, update place_type/attributes.

    Runs in a single transaction so a crash mid-script leaves the DB clean.

    Idempotent: if the Alembic migration has already dropped the legacy
    columns, this function exits cleanly as a no-op (nothing left to
    relocate). The operator can re-run the script any number of times
    against a fully migrated DB without errors.
    """
    # Self-provision the new columns if the DB is still at the legacy
    # (pre-feature-019) schema. IF NOT EXISTS means this is a no-op on a DB
    # that already has them, and the Alembic migration below this step has
    # been made idempotent in the same way so both orderings work.
    for stmt in (
        "ALTER TABLE places ADD COLUMN IF NOT EXISTS place_type VARCHAR",
        "ALTER TABLE places ADD COLUMN IF NOT EXISTS subcategory VARCHAR",
        "ALTER TABLE places ADD COLUMN IF NOT EXISTS tags JSONB",
        "ALTER TABLE places ADD COLUMN IF NOT EXISTS attributes JSONB",
        "ALTER TABLE places ADD COLUMN IF NOT EXISTS provider_id VARCHAR",
    ):
        await session.execute(text(stmt))

    # Which legacy columns still exist? The Alembic migration drops them,
    # so after a full upgrade the information_schema returns an empty set
    # and this script becomes a no-op (T062 — idempotency).
    legacy_cols_result = await session.execute(
        text(
            """
            SELECT column_name
              FROM information_schema.columns
             WHERE table_name = 'places'
               AND column_name IN (
                   'cuisine', 'price_range', 'ambiance',
                   'lat', 'lng', 'address',
                   'external_provider', 'external_id'
               )
            """
        )
    )
    legacy_cols = {row[0] for row in legacy_cols_result.fetchall()}

    if not legacy_cols:
        # DB is fully migrated — nothing to relocate. This is the normal
        # case on any re-run after the Alembic migration has landed.
        logger.info(
            "seed_migration: legacy columns already dropped, nothing to relocate"
        )
        return

    # Build the SELECT list dynamically, using NULL placeholders for any
    # legacy column that has already been dropped.
    def _col_or_null(name: str) -> str:
        return name if name in legacy_cols else f"NULL AS {name}"

    select_sql = f"""
        SELECT id, place_name,
               {_col_or_null("cuisine")},
               {_col_or_null("price_range")},
               {_col_or_null("ambiance")},
               {_col_or_null("lat")},
               {_col_or_null("lng")},
               {_col_or_null("address")},
               {_col_or_null("external_provider")},
               {_col_or_null("external_id")},
               place_type, attributes, provider_id
          FROM places
    """
    result = await session.execute(text(select_sql))
    rows = result.mappings().all()

    config = get_config()
    geo_ttl_seconds = config.places.cache_ttl_days * 86400

    geo_writes: dict[str, GeoData] = {}

    for row in rows:
        counters.scanned += 1
        row_id = row["id"]
        place_name = row["place_name"] or ""

        # ------------------------------------------------------------------
        # provider_id backfill (also handled by Alembic, but we do it first so
        # the geo cache seed below sees it).
        # ------------------------------------------------------------------
        provider_id: str | None = row["provider_id"]
        ext_provider = row["external_provider"]
        ext_id = row["external_id"]
        if provider_id is None and ext_provider and ext_id:
            provider_id = f"{ext_provider}:{ext_id}"
            await session.execute(
                text("UPDATE places SET provider_id = :pid WHERE id = :rid"),
                {"pid": provider_id, "rid": row_id},
            )

        # ------------------------------------------------------------------
        # attributes JSONB relocation (idempotent — skip fields already set)
        # ------------------------------------------------------------------
        existing_attrs: dict[str, Any] = row["attributes"] or {}
        attrs_updated = False

        if row["cuisine"] and "cuisine" not in existing_attrs:
            existing_attrs["cuisine"] = row["cuisine"]
            counters.cuisine_relocated += 1
            attrs_updated = True

        if row["price_range"] and "price_hint" not in existing_attrs:
            hint = map_price_hint(row["price_range"])
            if hint is not None:
                existing_attrs["price_hint"] = hint
                counters.price_mapped += 1
                attrs_updated = True
            else:
                counters.price_unmapped += 1
                counters.unmapped_prices.append((row_id, str(row["price_range"])))
                logger.warning(
                    "unmapped_price_range",
                    extra={"row_id": row_id, "price_range": row["price_range"]},
                )

        if row["ambiance"] and "ambiance" not in existing_attrs:
            existing_attrs["ambiance"] = row["ambiance"]
            counters.ambiance_relocated += 1
            attrs_updated = True

        if attrs_updated:
            await session.execute(
                text(
                    "UPDATE places SET attributes = CAST(:attrs AS JSONB) "
                    "WHERE id = :rid"
                ),
                {"attrs": json.dumps(existing_attrs), "rid": row_id},
            )

        # ------------------------------------------------------------------
        # place_type backfill
        # ------------------------------------------------------------------
        if row["place_type"] is None:
            place_type, defaulted = infer_place_type(place_name, row["cuisine"])
            await session.execute(
                text("UPDATE places SET place_type = :pt WHERE id = :rid"),
                {"pt": place_type, "rid": row_id},
            )
            if place_type == "food_and_drink":
                counters.place_type_food_and_drink += 1
            elif place_type == "things_to_do":
                counters.place_type_things_to_do += 1
            if defaulted:
                counters.place_type_defaulted += 1
                counters.defaulted_rows.append((row_id, place_name))
                logger.warning(
                    "place_type_defaulted",
                    extra={"row_id": row_id, "place_name": place_name},
                )

        # ------------------------------------------------------------------
        # Tier 2 geo cache seed — only for rows with a provider_id
        # ------------------------------------------------------------------
        lat = row["lat"]
        lng = row["lng"]
        address = row["address"]
        if lat is not None and lng is not None and address:
            if provider_id is not None:
                geo_writes[provider_id] = GeoData(
                    lat=float(lat),
                    lng=float(lng),
                    address=str(address),
                    cached_at=datetime.now(UTC),
                )
            else:
                counters.geo_lost_no_pid += 1
                counters.lost_geo_rows.append((row_id, place_name))
                logger.warning(
                    "geo_data_lost_no_provider_id",
                    extra={"row_id": row_id, "place_name": place_name},
                )

    # Commit all DB updates.
    await session.commit()

    # ------------------------------------------------------------------
    # One Redis pipeline for the whole geo seed batch
    # ------------------------------------------------------------------
    if geo_writes:
        async with redis_client.pipeline(transaction=False) as pipe:
            for pid, geo in geo_writes.items():
                key = f"places:geo:{pid}"
                pipe.set(key, geo.model_dump_json(), ex=geo_ttl_seconds)
            await pipe.execute()
        counters.geo_seeded = len(geo_writes)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def print_report(counters: Counters) -> None:
    print(f"seed_migration: scanned {counters.scanned} rows")
    print(f"  cuisine relocated:        {counters.cuisine_relocated}")
    print(
        f"  price_range mapped:       {counters.price_mapped}  "
        f"(unmapped: {counters.price_unmapped})"
    )
    print(f"  ambiance relocated:       {counters.ambiance_relocated}")
    print(f"  geo cache seeded:         {counters.geo_seeded}")
    print(f"  geo data lost (no pid):   {counters.geo_lost_no_pid}")
    total_inferred = (
        counters.place_type_food_and_drink
        + counters.place_type_things_to_do
        + counters.place_type_defaulted
    )
    print(
        f"  place_type inferred:      {total_inferred}  "
        f"(food_and_drink: {counters.place_type_food_and_drink}, "
        f"things_to_do: {counters.place_type_things_to_do}, "
        f"defaulted: {counters.place_type_defaulted})"
    )
    print("done.")

    if counters.place_type_defaulted > 0:
        print()
        print(
            f"⚠️  {counters.place_type_defaulted} rows were defaulted to "
            f"place_type='services'."
        )
        print(
            f"    Review {LOG_FILE} for \"place_type_defaulted\" lines BEFORE "
            f"running alembic upgrade head."
        )
        print(
            "    Each defaulted row has its id and place_name logged. Re-run "
            "extraction on those"
        )
        print(
            "    rows' source_url after deployment to derive a better place_type,"
        )
        print(
            "    OR re-run this script with --accept-defaults to clear the gate."
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main(accept_defaults: bool) -> int:
    logging.basicConfig(
        level=logging.INFO,
        filename=LOG_FILE,
        filemode="a",
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    from kebi.core.config import get_secrets

    secrets = get_secrets()
    db_url = secrets.DATABASE_URL
    if db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(db_url, future=True)
    redis_client = aioredis.from_url(secrets.REDIS_URL, decode_responses=False)

    counters = Counters()

    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            await _relocate(session, redis_client, counters)
        except Exception as exc:  # noqa: BLE001
            logger.exception("seed_migration failed")
            print(f"seed_migration: ERROR — {exc}", file=sys.stderr)
            await session.rollback()
            await redis_client.aclose()
            await engine.dispose()
            return 1

    await redis_client.aclose()
    await engine.dispose()

    print_report(counters)

    if counters.place_type_defaulted > 0 and not accept_defaults:
        logger.warning(
            "operator_review_required",
            extra={"defaulted_count": counters.place_type_defaulted},
        )
        return 2

    if counters.place_type_defaulted > 0 and accept_defaults:
        logger.info(
            "accepted_defaults",
            extra={"defaulted_count": counters.place_type_defaulted},
        )
        print()
        print(
            f"--accept-defaults: acknowledged {counters.place_type_defaulted} "
            f"defaulted rows. Proceeding."
        )

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Seed migration for feature 019 PlacesService."
    )
    parser.add_argument(
        "--accept-defaults",
        action="store_true",
        help="Acknowledge any place_type defaults and exit 0 instead of 2.",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(accept_defaults=args.accept_defaults)))
