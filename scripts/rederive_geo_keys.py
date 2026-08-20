"""Recompute `places.geo_key` through the geo registry.

Run after correcting registry data — repointing a row's `groups_into`,
fixing a wrong alias, adding splits to an ambiguous unit — so stored keys
catch up with the corrected identity. The hand-maintained fold tables this
script used to chase are gone; the registry rows are the rule now, and this
is the maintenance that storing a derived key still costs.

Deriving on read would avoid the job entirely, but the key has to be visible
to SQL — grouping a library by area and fetching one area's saves are both
set operations the database must do, not the application.

Idempotent, and safe to run against a live database: it only ever writes
`geo_key`, and only where the derived value differs from what is stored.

    poetry run python -m scripts.rederive_geo_keys
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from scripts.migrate_geo_identity import _rederive_places

from kebi.core.config import get_env
from kebi.core.geo.google_lookup import GoogleGeoLookupClient
from kebi.core.geo.registry import GeoRegistry
from kebi.db.repositories.geo_area_repository import SQLAlchemyGeoAreaRepository
from kebi.db.session import _get_session_factory
from kebi.providers import get_instructor_client

logger = logging.getLogger(__name__)


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    async with httpx.AsyncClient() as http:
        registry = GeoRegistry(
            repo=SQLAlchemyGeoAreaRepository(_get_session_factory()),
            lookup=GoogleGeoLookupClient(
                api_key=get_env().GOOGLE_API_KEY or "", http=http
            ),
            instructor_client=get_instructor_client("area_registry"),
        )
        async with _get_session_factory()() as session:
            changed = await _rederive_places(session, registry, dry_run=False)
    logger.info("re-derived geo_key on %d row(s)", changed)


if __name__ == "__main__":
    asyncio.run(_main())
