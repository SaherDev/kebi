"""Tests for EventHandlers.on_content_harvest_requested (ADR-121/122)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from kebi.core.events.events import AreaInterestNoted, ContentHarvestRequested
from kebi.core.events.handlers import EventHandlers
from kebi.core.knowledge.schemas import (
    HarvestContent,
    HarvestPlace,
    HarvestSnapshot,
    ResolvedGeo,
    StructuredClaim,
)

_SNAPSHOT = HarvestSnapshot(
    content=HarvestContent(caption="ramen"),
    places=[
        HarvestPlace(place_id="p1", name="Fuji", geo=ResolvedGeo(country_code="jp"))
    ],
)
_EVENT = ContentHarvestRequested(
    user_id="u1", harvest_key="harvest/r1.json", source_ref="u"
)


def _handlers(*, reader=None, harvester=None, ingestion=None) -> EventHandlers:
    return EventHandlers(
        taste_service=AsyncMock(),
        memory_service=AsyncMock(),
        intent_service=AsyncMock(),
        tracer=MagicMock(),
        harvest_reader=reader,
        harvester=harvester,
        ingestion=ingestion,
    )


async def test_reads_snapshot_harvests_and_ingests() -> None:
    reader = AsyncMock()
    reader.get = AsyncMock(return_value=_SNAPSHOT)
    claim = StructuredClaim(
        scope="place", entity_name="Fuji", claim="good", confidence=0.7, place_ref="p1"
    )
    harvester = AsyncMock()
    harvester.harvest = AsyncMock(return_value=[claim])
    ingestion = AsyncMock()
    ingestion.ingest = AsyncMock(return_value=[claim])

    await _handlers(
        reader=reader, harvester=harvester, ingestion=ingestion
    ).on_content_harvest_requested(_EVENT)

    reader.get.assert_awaited_once_with("harvest/r1.json")
    harvester.harvest.assert_awaited_once()
    # The producer is handed to ingestion, which stamps its provenance.
    args, kwargs = ingestion.ingest.call_args
    assert args[0] is harvester
    assert kwargs["source_ref"] == "u"
    assert kwargs["user_id"] is None  # harvested claims are global


async def test_missing_snapshot_is_noop() -> None:
    reader = AsyncMock()
    reader.get = AsyncMock(return_value=None)
    harvester = AsyncMock()
    ingestion = AsyncMock()

    await _handlers(
        reader=reader, harvester=harvester, ingestion=ingestion
    ).on_content_harvest_requested(_EVENT)

    harvester.harvest.assert_not_called()
    ingestion.ingest.assert_not_called()


async def test_failure_is_swallowed() -> None:
    reader = AsyncMock()
    reader.get = AsyncMock(side_effect=RuntimeError("bucket down"))
    # Must not raise (ADR-043).
    await _handlers(
        reader=reader, harvester=AsyncMock(), ingestion=AsyncMock()
    ).on_content_harvest_requested(_EVENT)


# ---------------------------------------------------------------------------
# Location-kinds Step 3 — the harvest handler also trains taste from the
# share's noted areas (region interest) and experience-tagged claims.
# ---------------------------------------------------------------------------

from kebi.core.areas.models import AreaEntity  # noqa: E402
from kebi.core.knowledge.schemas import NotedAreaRef  # noqa: E402

_NOTED_SNAPSHOT = HarvestSnapshot(
    content=HarvestContent(caption="3 stops in Vietnam"),
    places=[],
    noted_areas=[
        NotedAreaRef(name="Hoi An", country_code="vn", reason="non_venue_area"),
        NotedAreaRef(name="Ha Giang Loop", country_code="vn", reason="non_venue_route"),
    ],
)


def _area(entity_key: str, name: str) -> AreaEntity:
    return AreaEntity(
        entity_key=entity_key,
        entity_type="city",
        name=name,
        country_code="vn",
        lat=0.0,
        lng=0.0,
    )


async def test_emits_region_signal_per_resolved_area() -> None:
    """Region interest is written by its own handler, so it does not depend on
    the harvest having anything to mine."""
    harvester = AsyncMock()
    harvester.resolve_area_interests = AsyncMock(
        return_value=[_area("vn/hoi-an", "Hoi An"), _area("vn/ha-giang", "Ha Giang")]
    )
    taste = AsyncMock()

    handlers = _handlers(harvester=harvester)
    handlers.taste_service = taste
    await handlers.on_area_interest_noted(
        AreaInterestNoted(user_id="u1", noted_areas=_NOTED_SNAPSHOT.noted_areas)
    )

    assert taste.handle_area_signal.await_count == 2
    keys = {c.args[1] for c in taste.handle_area_signal.await_args_list}
    assert keys == {"vn/hoi-an", "vn/ha-giang"}


async def test_harvest_handler_does_not_also_write_region_signals() -> None:
    """The region half moved out of the harvest handler. If it were emitted in
    both places, every share would write its region signal twice."""
    reader = AsyncMock()
    reader.get = AsyncMock(return_value=_NOTED_SNAPSHOT)
    harvester = AsyncMock()
    harvester.harvest = AsyncMock(return_value=[])
    harvester.resolve_area_interests = AsyncMock(
        return_value=[_area("vn/hoi-an", "Hoi An")]
    )
    ingestion = AsyncMock()
    ingestion.ingest = AsyncMock(return_value=[])
    taste = AsyncMock()

    handlers = _handlers(reader=reader, harvester=harvester, ingestion=ingestion)
    handlers.taste_service = taste
    await handlers.on_content_harvest_requested(_EVENT)

    taste.handle_area_signal.assert_not_awaited()


async def test_area_signal_failure_is_swallowed() -> None:
    """Best-effort like its siblings (ADR-043) — the place work already
    succeeded, so a taste failure must not surface."""
    harvester = AsyncMock()
    harvester.resolve_area_interests = AsyncMock(side_effect=RuntimeError("boom"))

    handlers = _handlers(harvester=harvester)
    handlers.taste_service = AsyncMock()
    await handlers.on_area_interest_noted(
        AreaInterestNoted(user_id="u1", noted_areas=_NOTED_SNAPSHOT.noted_areas)
    )


async def test_experience_signal_from_experience_tagged_claims() -> None:
    reader = AsyncMock()
    reader.get = AsyncMock(return_value=_NOTED_SNAPSHOT)
    claims = [
        StructuredClaim(
            scope="city",
            entity_name="Ha Giang",
            claim="A motorbike loop through karst mountains.",
            tags=["motorbike_route", "scenic_route", "cheap_eats"],
            confidence=0.8,
            geo=ResolvedGeo(country_code="vn", city="Ha Giang"),
        )
    ]
    harvester = AsyncMock()
    harvester.harvest = AsyncMock(return_value=claims)
    harvester.resolve_area_interests = AsyncMock(return_value=[])
    ingestion = AsyncMock()
    ingestion.ingest = AsyncMock(return_value=claims)
    taste = AsyncMock()

    handlers = _handlers(reader=reader, harvester=harvester, ingestion=ingestion)
    handlers.taste_service = taste
    await handlers.on_content_harvest_requested(_EVENT)

    # Only the experience-type tags are lifted into the experience signal.
    taste.handle_experience_signal.assert_awaited_once()
    _, exps = taste.handle_experience_signal.await_args.args
    assert set(exps) == {"motorbike_route", "scenic_route"}
    assert "cheap_eats" not in exps
