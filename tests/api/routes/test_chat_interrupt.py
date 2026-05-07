"""Tests for GraphInterrupt → clarification mapping in ChatService (feature 028 M8)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from langgraph.errors import GraphInterrupt

from kebi.api.deps import get_chat_service
from kebi.api.main import app
from kebi.api.schemas.chat import ChatRequest
from kebi.core.chat.service import ChatService
from kebi.core.config import AgentConfig, AppConfig


def _make_mock_config(enabled: bool = True) -> MagicMock:
    cfg = MagicMock(spec=AppConfig)
    agent_cfg = MagicMock(spec=AgentConfig)
    agent_cfg.enabled = enabled
    cfg.agent = agent_cfg
    return cfg


def _make_interrupt_service(place_name: str = "Fuji Ramen") -> MagicMock:
    """ChatService mock that raises GraphInterrupt on run()."""
    svc = MagicMock(spec=ChatService)
    interrupt_payload = {
        "type": "save_needs_review",
        "request_id": "req-abc",
        "candidates": [{"place": {"place_name": place_name}, "confidence": 0.4}],
    }
    svc.run = AsyncMock(side_effect=GraphInterrupt(interrupt_payload))
    return svc


@pytest.fixture
def interrupt_client() -> TestClient:
    interrupt_svc = _make_interrupt_service("Thai Garden")
    app.dependency_overrides[get_chat_service] = lambda: interrupt_svc
    yield TestClient(app)
    app.dependency_overrides.pop(get_chat_service, None)


class TestChatInterruptMapping:
    """Verify ChatService maps GraphInterrupt to clarification response."""

    async def test_graph_interrupt_maps_to_clarification_response(self) -> None:
        """_run_agent catches GraphInterrupt and returns type='clarification'."""
        interrupt_payload = {
            "type": "save_needs_review",
            "request_id": "req-1",
            "candidates": [
                {"place": {"place_name": "Yamazaki Bar"}, "confidence": 0.4}
            ],
        }

        mock_graph = MagicMock()
        mock_graph.ainvoke = AsyncMock(side_effect=GraphInterrupt(interrupt_payload))

        cfg = _make_mock_config(enabled=True)
        places_mock = MagicMock()
        places_mock.resolve_location_label = AsyncMock(return_value=None)
        svc = ChatService(
            extraction_service=MagicMock(),
            consult_service=MagicMock(),
            recall_service=MagicMock(),
            event_dispatcher=MagicMock(dispatch=AsyncMock()),
            memory_service=MagicMock(),
            taste_service=MagicMock(),
            places_service=places_mock,
            config=cfg,
            agent_graph=mock_graph,
        )
        svc._compose_taste_summary = AsyncMock(return_value="")
        svc._compose_memory_summary = AsyncMock(return_value="")

        request = ChatRequest(user_id="u1", message="save https://example.com/video")
        result = await svc._run_agent(request)

        assert result.type == "clarification"
        assert "Yamazaki Bar" in result.message
        assert result.data is not None
        assert "interrupt" in result.data

    async def test_clarification_message_falls_back_for_empty_candidates(
        self,
    ) -> None:
        """When candidates list is empty, place name defaults to 'this place'."""
        interrupt_payload = {
            "type": "save_needs_review",
            "request_id": "req-2",
            "candidates": [],
        }

        mock_graph = MagicMock()
        mock_graph.ainvoke = AsyncMock(side_effect=GraphInterrupt(interrupt_payload))

        cfg = _make_mock_config(enabled=True)
        places_mock = MagicMock()
        places_mock.resolve_location_label = AsyncMock(return_value=None)
        svc = ChatService(
            extraction_service=MagicMock(),
            consult_service=MagicMock(),
            recall_service=MagicMock(),
            event_dispatcher=MagicMock(dispatch=AsyncMock()),
            memory_service=MagicMock(),
            taste_service=MagicMock(),
            places_service=places_mock,
            config=cfg,
            agent_graph=mock_graph,
        )
        svc._compose_taste_summary = AsyncMock(return_value="")
        svc._compose_memory_summary = AsyncMock(return_value="")

        request = ChatRequest(user_id="u1", message="save https://example.com/video")
        result = await svc._run_agent(request)

        assert result.type == "clarification"
        assert "this place" in result.message
