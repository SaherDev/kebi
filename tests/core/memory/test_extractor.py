"""Unit tests for MemoryExtractor (LLM-backed personal-fact extraction)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from totoro_ai.core.memory.extractor import MemoryExtractor, _FactsResponse
from totoro_ai.core.memory.schemas import PersonalFact


def _make_tracer() -> MagicMock:
    span = MagicMock()
    span.end = MagicMock()
    tracer = MagicMock()
    tracer.generation = MagicMock(return_value=span)
    tracer.capture_message = MagicMock()
    tracer.flush = MagicMock()
    return tracer


async def test_extract_returns_facts_for_stated_message() -> None:
    fact = PersonalFact(text="I'm vegetarian", source="stated")
    client = MagicMock()
    client.extract = AsyncMock(return_value=_FactsResponse(facts=[fact]))
    tracer = _make_tracer()

    extractor = MemoryExtractor(instructor_client=client, tracer=tracer)
    result = await extractor.extract("I'm vegetarian", user_id="u1")

    assert result == [fact]
    client.extract.assert_awaited_once()
    span = tracer.generation.return_value
    span.end.assert_called_once()


async def test_extract_returns_empty_on_llm_exception() -> None:
    client = MagicMock()
    client.extract = AsyncMock(side_effect=RuntimeError("boom"))
    tracer = _make_tracer()

    extractor = MemoryExtractor(instructor_client=client, tracer=tracer)
    result = await extractor.extract("hello", user_id="u1")

    assert result == []
    span = tracer.generation.return_value
    span.end.assert_called_once()
    tracer.capture_message.assert_called_once()


async def test_extract_filters_non_stated_sources() -> None:
    """Defensive: drop facts where source != 'stated' even if model emits them."""
    facts = [
        PersonalFact(text="I'm vegan", source="stated"),
        PersonalFact(text="seems lactose intolerant", source="inferred"),
    ]
    client = MagicMock()
    client.extract = AsyncMock(return_value=_FactsResponse(facts=facts))
    tracer = _make_tracer()

    extractor = MemoryExtractor(instructor_client=client, tracer=tracer)
    result = await extractor.extract("I'm vegan", user_id="u1")

    assert len(result) == 1
    assert result[0].source == "stated"
    assert result[0].text == "I'm vegan"
