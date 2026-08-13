"""The chat wire contract: text plus entity links, no per-tool cards (ADR-136).

These lock the shape the client renders against — that the message comes back
linkified, that `data` carries `entities` and no tool payloads, and that
`recommendation_id` survived the removal of `tool_results` so a later
accept/reject can still be attributed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import AIMessage

from kebi.api.schemas.chat import ChatRequest
from kebi.core.areas.keys import encode_area_id
from kebi.core.chat.service import ChatService

# Area URIs carry the geo key encoded as one opaque segment (ADR-153).
_CANGGU_URI = f"kebi://area/{encode_area_id('id/badung/canggu')}"

_CANGGU = {
    "country": "Indonesia",
    "country_code": "id",
    "city": "Badung",
    "neighborhood": "Canggu",
    "lat": -8.65,
    "lng": 115.13,
    "neighborhood_icon": "🏄",
}


def _tool_results() -> list[dict[str, Any]]:
    return [
        {
            "tool": "find_saved",
            "tool_call_id": "call-1",
            "payload": {
                "recommendation_id": "rec-99",
                "candidates": [
                    {
                        "place": {
                            "id": "p1",
                            "place_name": "Luigis",
                            "icon": "🍕",
                        },
                        "source": "saved",
                    }
                ],
            },
        }
    ]


def _graph(answer: str) -> MagicMock:
    """Stub the two-snapshot stream: tool results, then the final state."""

    def _factory(*_a: Any, **_kw: Any) -> AsyncIterator[dict[str, Any]]:
        async def _gen() -> AsyncIterator[dict[str, Any]]:
            yield {
                "messages": [],
                "reasoning_steps": [],
                "working_location": _CANGGU,
                "tool_results": _tool_results(),
            }
            yield {
                "messages": [AIMessage(content=answer)],
                "reasoning_steps": [],
                "working_location": _CANGGU,
                "tool_results": [],
                "tool_calls_used": 1,
            }

        return _gen()

    graph = AsyncMock()
    graph.astream = MagicMock(side_effect=_factory)
    return graph


def _service(graph: MagicMock) -> ChatService:
    from kebi.core.config import get_config

    taste_service = AsyncMock()
    taste_service.get_taste_profile = AsyncMock(return_value=None)
    memory_service = AsyncMock()
    memory_service.load_memories = AsyncMock(return_value=[])
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()
    return ChatService(
        event_dispatcher=dispatcher,
        memory_service=memory_service,
        taste_service=taste_service,
        config=get_config().model_copy(deep=True),
        agent_graph=graph,
    )


async def _run(answer: str) -> Any:
    return await _service(_graph(answer)).run(
        ChatRequest(message="where should i go tonight"), user_id="u-1"
    )


async def test_place_names_come_back_as_links() -> None:
    result = await _run("its monday so tonight is Luigis night")
    assert result.message == (
        "its monday so tonight is [Luigis](kebi://venue/p1) night"
    )


async def test_area_names_link_too() -> None:
    result = await _run("monday is their big night in Canggu")
    assert f"[Canggu]({_CANGGU_URI})" in result.message


async def test_entities_resolve_each_link() -> None:
    result = await _run("tonight is Luigis night in Canggu")
    entities = result.data["entities"]
    assert entities == [
        {
            "kind": "venue",
            "key": "p1",
            "name": "Luigis",
            "uri": "kebi://venue/p1",
            "icon": "🍕",
        },
        {
            "kind": "area",
            "key": "id/badung/canggu",
            "name": "Canggu",
            "uri": _CANGGU_URI,
            # Row-sourced (ADR-162): the resolver's per-turn pick no longer
            # rides entities, and this service has no refresher wired — so
            # the area ships icon-less and the client falls back.
            "icon": None,
        },
    ]


async def test_no_per_tool_payloads_cross_the_wire() -> None:
    result = await _run("tonight is Luigis night")
    assert "tool_results" not in result.data
    assert set(result.data) == {"reasoning_steps", "entities", "recommendation_id"}


async def test_recommendation_id_survives_for_signal_attribution() -> None:
    result = await _run("tonight is Luigis night")
    assert result.data["recommendation_id"] == "rec-99"


async def test_an_answer_naming_nothing_retrieved_is_untouched() -> None:
    result = await _run("no idea honestly")
    assert result.message == "no idea honestly"
    assert result.data["entities"] == []
