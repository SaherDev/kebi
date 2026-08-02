"""Shared fixtures for agent-graph tests (feature 027 M3)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from kebi.core.agent.location import LocationResolution


@pytest.fixture
def checkpointer() -> InMemorySaver:
    """In-memory checkpointer — structural tests do not hit Postgres."""
    return InMemorySaver()


@pytest.fixture
def mock_llm() -> MagicMock:
    """Fake chat model compatible with `agent_node`'s contract.

    - `.bind_tools(tools)` returns self (so the chain can still `.ainvoke`).
    - `.ainvoke(messages)` returns a preconfigured AIMessage (override in tests).

    Tests that need a specific response set `mock_llm.ainvoke.return_value`
    before invoking the graph/node.
    """
    llm = MagicMock()
    llm.bind_tools = MagicMock(return_value=llm)

    async def _default_ainvoke(_messages: Any) -> AIMessage:
        return AIMessage(content="mock response")

    llm.ainvoke = MagicMock(side_effect=_default_ainvoke)
    return llm


@pytest.fixture
def no_tools() -> list[Any]:
    """Empty tool list — structural tests don't exercise real tools."""
    return []


@pytest.fixture
def mock_resolver_llm() -> MagicMock:
    """Fake location-resolver chat model.

    `.with_structured_output(...)` returns a runnable whose `.ainvoke`
    yields a preconfigured `LocationResolution`. Tests override via
    `mock_resolver_llm._structured.ainvoke.side_effect`.
    """
    llm = MagicMock()
    structured = MagicMock()

    async def _default(_messages: Any) -> LocationResolution:
        return LocationResolution(
            source="user_actual",
            needs_clarification=True,
            clarification_reason="no location",
        )

    structured.ainvoke = MagicMock(side_effect=_default)
    llm.with_structured_output = MagicMock(return_value=structured)
    llm._structured = structured
    return llm


@pytest.fixture
def mock_geocoding_client() -> MagicMock:
    """Fake geocoding boundary — search_area/reverse/geocode_place_id are
    AsyncMocks returning `GeocodeResult`s (ADR-084)."""
    from kebi.providers.geocoding import GeocodeResult

    client = MagicMock()
    client.search_area = AsyncMock(
        return_value=GeocodeResult(lat=13.75, lng=100.5, place_type="city")
    )
    client.geocode_place_id = AsyncMock(return_value=None)
    client.reverse = AsyncMock(
        return_value=GeocodeResult(
            lat=13.75,
            lng=100.5,
            country="Thailand",
            city="Bangkok",
            place_type="city",
        )
    )
    return client


@pytest.fixture
def mock_area_service() -> MagicMock:
    """Fake AreaService — any named country/city resolves to an entity."""
    from kebi.core.areas.models import AreaEntity

    svc = MagicMock()

    async def _country(name: str) -> AreaEntity:
        return AreaEntity(
            entity_key="th",
            entity_type="country",
            name=name,
            country_code="th",
            lat=13.75,
            lng=100.5,
        )

    async def _city(name: str, cc: str) -> AreaEntity:
        return AreaEntity(
            entity_key=f"{cc}/{name.lower().replace(' ', '-')}",
            entity_type="city",
            name=name,
            country_code=cc,
            lat=13.75,
            lng=100.5,
            place_type="locality",
        )

    svc.resolve_country = AsyncMock(side_effect=_country)
    svc.resolve_city = AsyncMock(side_effect=_city)
    return svc
