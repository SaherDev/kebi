"""Tests for the ClaimProducer seam + KnowledgeIngestion (ADR-122)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from kebi.core.knowledge.curator import KnowledgeCurator
from kebi.core.knowledge.harvester import KnowledgeHarvester
from kebi.core.knowledge.producer import ClaimProducer, KnowledgeIngestion
from kebi.core.knowledge.schemas import ResolvedGeo, StructuredClaim

_CLAIM = StructuredClaim(
    scope="city",
    entity_name="Dubai",
    claim="nightlife peaks late",
    confidence=0.8,
    geo=ResolvedGeo(country_code="ae", city="Dubai"),
)


def test_harvester_and_curator_are_claim_producers() -> None:
    harvester = KnowledgeHarvester(AsyncMock(), confidence_floor=0.35)
    curator = KnowledgeCurator(AsyncMock(), AsyncMock(), confidence_floor=0.9)
    assert isinstance(harvester, ClaimProducer)
    assert isinstance(curator, ClaimProducer)
    assert harvester.source_type == "shared_content"
    assert curator.source_type == "curated_expert"


async def test_ingestion_stamps_producer_provenance() -> None:
    writer = AsyncMock()
    writer.persist = AsyncMock(return_value=[_CLAIM])

    # A minimal producer — any object exposing the three provenance fields.
    class _Producer:
        source_type = "curated_expert"
        confidence_floor = 0.9
        review_status = "pending"

    written = await KnowledgeIngestion(writer).ingest(
        _Producer(), [_CLAIM], source_ref="curator:user_x", user_id=None
    )

    assert written == [_CLAIM]
    _, kwargs = writer.persist.call_args
    assert kwargs["source_type"] == "curated_expert"
    assert kwargs["confidence_floor"] == 0.9
    assert kwargs["review_status"] == "pending"
    assert kwargs["source_ref"] == "curator:user_x"
    assert kwargs["user_id"] is None
