"""The web-search flywheel: the background half (ADR-145).

The handler runs after the answer is already sent, so its defining property
is that nothing it does can disturb a turn that already succeeded.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from kebi.core.events.events import WebFindingsHarvestRequested
from kebi.core.events.handlers import EventHandlers, _web_source_ref
from kebi.core.knowledge.schemas import StructuredClaim
from kebi.core.web.models import WebSearchResult

_RESULT: dict[str, Any] = {
    "query": "atm fees Canggu",
    "findings": [{"text": "BNI charges no withdrawal fee."}],
    "country_code": "id",
    "city": "Badung",
    "neighborhood": "Canggu",
}


def _claim() -> StructuredClaim:
    return StructuredClaim(
        scope="neighborhood",
        entity_name="Canggu",
        claim="BNI ATMs here charge no withdrawal fee.",
        confidence=0.7,
    )


def _handlers(
    *,
    harvester: Any = None,
    ingestion: Any = None,
) -> tuple[EventHandlers, Any, Any]:
    if harvester is None:
        harvester = MagicMock()
        harvester.harvest = AsyncMock(return_value=[_claim()])
    if ingestion is None:
        ingestion = MagicMock()
        ingestion.ingest = AsyncMock(return_value=[_claim()])
    handlers = EventHandlers(
        taste_service=MagicMock(),
        memory_service=MagicMock(),
        intent_service=MagicMock(),
        tracer=MagicMock(),
        ingestion=ingestion,
        web_harvester=harvester,
    )
    return handlers, harvester, ingestion


def _event(result: dict[str, Any] | None = None) -> WebFindingsHarvestRequested:
    return WebFindingsHarvestRequested(user_id="u-1", result=result or dict(_RESULT))


async def test_findings_are_mined_and_written() -> None:
    handlers, harvester, ingestion = _handlers()
    await handlers.on_web_findings_harvest_requested(_event())
    assert isinstance(harvester.harvest.await_args.args[0], WebSearchResult)
    ingestion.ingest.assert_awaited_once()


async def test_a_web_claim_is_global_not_scoped_to_the_asker() -> None:
    """A fact about an area belongs to everyone who asks about that area —
    that is the whole point of writing it back."""
    handlers, _, ingestion = _handlers()
    await handlers.on_web_findings_harvest_requested(_event())
    assert ingestion.ingest.await_args.kwargs["user_id"] is None


async def test_provenance_is_the_query_not_one_page() -> None:
    """A claim is usually synthesised from several snippets, so pinning it to
    one URL would be a citation that does not hold."""
    handlers, _, ingestion = _handlers()
    await handlers.on_web_findings_harvest_requested(_event())
    ref = ingestion.ingest.await_args.kwargs["source_ref"]
    assert ref == "web_search:atm fees Canggu"


def test_a_pathological_query_cannot_overflow_the_source_ref_column() -> None:
    ref = _web_source_ref(WebSearchResult(query="x" * 5000))
    assert len(ref) <= 500


async def test_nothing_durable_means_no_write() -> None:
    harvester = MagicMock()
    harvester.harvest = AsyncMock(return_value=[])
    handlers, _, ingestion = _handlers(harvester=harvester)
    await handlers.on_web_findings_harvest_requested(_event())
    ingestion.ingest.assert_not_awaited()


async def test_an_unwired_harvester_is_a_no_op() -> None:
    handlers = EventHandlers(
        taste_service=MagicMock(),
        memory_service=MagicMock(),
        intent_service=MagicMock(),
        tracer=MagicMock(),
    )
    await handlers.on_web_findings_harvest_requested(_event())


async def test_a_harvest_failure_never_escapes() -> None:
    """The user already has their answer. Nothing here may raise (ADR-043)."""
    harvester = MagicMock()
    harvester.harvest = AsyncMock(side_effect=RuntimeError("model down"))
    handlers, _, _ = _handlers(harvester=harvester)
    await handlers.on_web_findings_harvest_requested(_event())


async def test_an_unparseable_payload_never_escapes() -> None:
    handlers, _, ingestion = _handlers()
    await handlers.on_web_findings_harvest_requested(
        _event({"query": 42, "findings": "not a list"})
    )
    ingestion.ingest.assert_not_awaited()
