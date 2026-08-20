"""Unit tests for MemoryExtractor (LLM-backed personal-fact extraction).

Tracing infrastructure is tested in `tests/providers/test_tracing.py`
and `tests/core/agent/test_trace_context.py`. Here we only verify the
extraction behavior — facts returned, exception swallowed, sources
filtered.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from kebi.core.memory.extractor import MemoryExtractor, _FactsResponse
from kebi.core.memory.schemas import PersonalFact
from kebi.providers.llm import InstructorExtraction


async def test_extract_returns_facts_for_stated_message() -> None:
    fact = PersonalFact(text="I'm vegetarian", source="stated")
    client = MagicMock()
    client.extract = AsyncMock(
        return_value=InstructorExtraction(data=_FactsResponse(facts=[fact]))
    )

    extractor = MemoryExtractor(instructor_client=client)
    result = await extractor.extract("I'm vegetarian", user_id="u1")

    assert result == [fact]
    client.extract.assert_awaited_once()


async def test_extract_returns_empty_on_llm_exception() -> None:
    client = MagicMock()
    client.extract = AsyncMock(side_effect=RuntimeError("boom"))

    extractor = MemoryExtractor(instructor_client=client)
    result = await extractor.extract("hello", user_id="u1")

    assert result == []


async def test_extract_filters_non_stated_sources() -> None:
    """Defensive: drop facts where source != 'stated' even if model emits them."""
    facts = [
        PersonalFact(text="I'm vegan", source="stated"),
        PersonalFact(text="seems lactose intolerant", source="inferred"),
    ]
    client = MagicMock()
    client.extract = AsyncMock(
        return_value=InstructorExtraction(data=_FactsResponse(facts=facts))
    )

    extractor = MemoryExtractor(instructor_client=client)
    result = await extractor.extract("I'm vegan", user_id="u1")

    assert len(result) == 1
    assert result[0].source == "stated"
    assert result[0].text == "I'm vegan"
