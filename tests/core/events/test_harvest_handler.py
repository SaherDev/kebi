"""Tests for EventHandlers.on_content_harvest_requested (ADR-121/122)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from kebi.core.events.events import ContentHarvestRequested
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


def _handlers(*, reader, harvester, ingestion) -> EventHandlers:
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
