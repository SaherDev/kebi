"""Live end-to-end regression for multi-place carousel extraction.

Pins the manual verification done for the resolve-then-search redesign
(ADR-080) + the session-per-query concurrency fix: the TikTok photo
carousel below references five Bangkok restaurants and historically
collapsed to one ("Restaurant POTONG") because the parallel search
fan-out shared a single non-concurrency-safe ``AsyncSession``. After
the fix all five resolve, location-biased to Bangkok.

This hits the real pipeline (TikTok scrape -> vision -> resolver ->
Google Places -> classifier) and needs DB + Redis + API keys, so it is
skipped unless explicitly enabled:

    KEBI_E2E_EXTRACTION=1 poetry run pytest \
        tests/core/extraction/test_multi_place_e2e.py -m e2e

The Google ``place_id`` is the stable join key (display names drift
with Google formatting), so the assertion is on the place_id set.
"""

from __future__ import annotations

import os

import pytest

from kebi.api import deps
from kebi.core.extraction.input_parser import parse_input
from kebi.db.session import _get_session_factory

# Verified 2026-05-19 against the live endpoint (cache cleared) — the
# five venues the carousel actually references, by Google place_id.
TEST_URL = (
    "https://www.tiktok.com/@withme808/photo/7620175392019664161?is_from_webapp=1"
)
EXPECTED_PLACE_IDS = {
    "ChIJd7grWxeZ4jAR8KwClMpGmHo",  # Restaurant POTONG
    "ChIJWfYJ3QWf4jAR9erXv7kK7sA",  # Sorn
    "ChIJmXuobsWY4jARc3EtJwWV21s",  # Mezzaluna
    "ChIJRxVBdTyf4jARGLWgEHjEIq4",  # Signature Bangkok
    "ChIJf8OOlnmZ4jAR1XxCVTOPfPY",  # Côte by Mauro Colagreco
}

pytestmark = pytest.mark.skipif(
    os.environ.get("KEBI_E2E_EXTRACTION") != "1",
    reason="live e2e: set KEBI_E2E_EXTRACTION=1 to run "
    "(needs network + DB + Redis + API keys)",
)


@pytest.mark.e2e
async def test_tiktok_carousel_resolves_all_five_bangkok_venues() -> None:
    parsed = parse_input(TEST_URL)
    session_factory = _get_session_factory()

    async with session_factory() as session:
        places_repo = deps.get_places_repo(db_session=session)
        embeddings_repo = deps.get_embeddings_repo(db_session=session)
        cache = deps.get_places_cache()
        gclient = deps.get_google_places_client()
        emb_service = deps.get_embedding_service(
            repo=embeddings_repo,
            embedder=deps.get_places_embedder(),
            config=deps.get_config(),
        )
        upsert = deps.get_place_upsert_service(
            repo=places_repo, embedding_service=emb_service
        )
        search_service = deps.get_places_search_service(
            repo=places_repo,
            cache=cache,
            client=gclient,
            upsert_service=upsert,
        )
        pipeline = deps.get_extraction_pipeline(
            extraction_config=deps.get_extraction_config(config=deps.get_config()),
            search_service=search_service,
            search_service_factory=deps.get_search_service_factory(
                cache=cache, client=gclient
            ),
        )

        results = await pipeline.run(
            url=parsed.url, user_id="e2e-multi-place", limit=25
        )

    returned_place_ids = {
        r.provider_id.split(":", 1)[1]
        for r in results
        if r.provider_id and ":" in r.provider_id
    }

    # Regression contract: the carousel must not collapse to one place,
    # and every verified Bangkok venue must come back.
    assert len(results) == 5, (
        f"expected 5 places, got {len(results)}: "
        f"{[(r.place_name, r.provider_id) for r in results]}"
    )
    assert returned_place_ids >= EXPECTED_PLACE_IDS, (
        "missing expected venues: "
        f"{EXPECTED_PLACE_IDS - returned_place_ids}; "
        f"got {[(r.place_name, r.provider_id) for r in results]}"
    )
