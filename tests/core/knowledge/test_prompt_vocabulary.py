"""The rendered claim-tag vocabulary reaches both writer LLM prompts.

The vocabulary is rendered from `core/knowledge/tags.py` into the system
message at call time, so the emitting model and the writer's
normalization can never drift. A practical-fact fixture round-trips: the
model's emitted vocab tags survive `_resolve` untouched (normalization
happens at the writer, covered in test_writer.py).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from kebi.core.knowledge.curator import KnowledgeCurator, _CuratorResponse
from kebi.core.knowledge.harvester import (
    KnowledgeHarvester,
    _HarvestedClaim,
    _HarvesterResponse,
)
from kebi.core.knowledge.schemas import (
    HarvestContent,
    HarvestPlace,
    ResolvedGeo,
)
from kebi.providers.llm import InstructorExtraction


def _harvest_inputs() -> tuple[HarvestContent, list[HarvestPlace]]:
    content = HarvestContent(
        caption="cash only, the ATM inside charges a fee", source_ref="post-1"
    )
    places = [
        HarvestPlace(
            place_id="p1",
            name="Banh Mi Corner",
            geo=ResolvedGeo(country_code="vn", city="Da Nang"),
        )
    ]
    return content, places


async def test_harvester_system_prompt_carries_the_vocabulary() -> None:
    client = AsyncMock()
    client.extract = AsyncMock(
        return_value=InstructorExtraction(data=_HarvesterResponse(claims=[]))
    )
    harvester = KnowledgeHarvester(client, AsyncMock())

    content, places = _harvest_inputs()
    await harvester.harvest(content, places)

    system = client.extract.await_args.kwargs["messages"][0]["content"]
    assert "Allowed tag values" in system
    assert "no_fee_atm" in system
    assert "timing_trick" in system


async def test_curator_system_prompt_carries_the_vocabulary() -> None:
    client = AsyncMock()
    client.extract = AsyncMock(
        return_value=InstructorExtraction(data=_CuratorResponse(claims=[]))
    )
    curator = KnowledgeCurator(client, AsyncMock())

    await curator.structure("Tipping is not expected in Da Nang.")

    system = client.extract.await_args.kwargs["messages"][0]["content"]
    assert "Allowed tag values" in system
    assert "tipping" in system


async def test_practical_fact_claim_keeps_vocab_tags_through_resolve() -> None:
    """The write→read seam: a practical fact tagged from the vocabulary
    comes out of the harvester as a StructuredClaim carrying those tags."""
    client = AsyncMock()
    client.extract = AsyncMock(
        return_value=InstructorExtraction(
            data=_HarvesterResponse(
                claims=[
                    _HarvestedClaim(
                        scope="place",
                        place_index=0,
                        entity_name="Banh Mi Corner",
                        claim="Cash only; the ATM inside charges a withdrawal fee.",
                        tags=["cash_only", "atm_fees"],
                        confidence=0.7,
                    )
                ]
            )
        )
    )
    harvester = KnowledgeHarvester(client, AsyncMock())

    content, places = _harvest_inputs()
    claims = await harvester.harvest(content, places)

    assert len(claims) == 1
    assert claims[0].tags == ["cash_only", "atm_fees"]
    assert claims[0].place_ref == "p1"
