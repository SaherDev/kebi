"""Tests for POST /v1/chat/stream SSE endpoint (feature 028 M7)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from kebi.api.deps import get_agent_graph, get_chat_service
from kebi.api.main import app
from kebi.core.agent.reasoning import ReasoningStep
from kebi.core.chat.service import ChatService
from kebi.core.config import AppConfig


def _make_mock_service() -> MagicMock:
    """Build a mock ChatService with async taste/memory helpers."""
    svc = MagicMock(spec=ChatService)
    svc._config = MagicMock(spec=AppConfig)
    svc._compose_taste_summary = AsyncMock(return_value="")
    svc._compose_memory_summary = AsyncMock(return_value="")
    svc._dispatcher = MagicMock(dispatch=AsyncMock())
    return svc



@pytest.fixture
def mock_service() -> MagicMock:
    return _make_mock_service()


@pytest.fixture
def mock_graph() -> MagicMock:
    """Fake compiled graph whose astream yields (stream_mode, chunk) tuples."""
    graph = MagicMock()

    async def _astream(
        payload: Any, config: Any, stream_mode: Any = None
    ) -> AsyncGenerator[tuple[str, Any], None]:
        rs = ReasoningStep(
            step="recall.search",
            summary="searching saves",
            source="tool",
            tool_name="recall",
            visibility="debug",
        )
        yield ("custom", rs.model_dump(mode="json"))

        from langchain_core.messages import AIMessage

        yield (
            "values",
            {
                "messages": [AIMessage(content="Here is my recommendation")],
                "tool_calls_used": 0,
            },
        )

    graph.astream = _astream
    return graph


@pytest.fixture
def client(mock_service: MagicMock, mock_graph: MagicMock) -> TestClient:
    app.dependency_overrides[get_chat_service] = lambda: mock_service
    app.dependency_overrides[get_agent_graph] = lambda: mock_graph
    yield TestClient(app)
    app.dependency_overrides.pop(get_chat_service, None)
    app.dependency_overrides.pop(get_agent_graph, None)


class TestChatStreamHappyPath:
    """Verify POST /v1/chat/stream returns SSE frames for the agent path."""

    def test_stream_returns_200(self, client: TestClient) -> None:
        response = client.post(
            "/v1/chat/stream",
            json={"user_id": "u1", "message": "dinner nearby"},
            headers={"Accept": "text/event-stream"},
        )
        assert response.status_code == 200

    def test_stream_content_type_is_event_stream(self, client: TestClient) -> None:
        response = client.post(
            "/v1/chat/stream",
            json={"user_id": "u1", "message": "dinner nearby"},
        )
        assert "text/event-stream" in response.headers["content-type"]

    def test_stream_contains_reasoning_step_frame(self, client: TestClient) -> None:
        response = client.post(
            "/v1/chat/stream",
            json={"user_id": "u1", "message": "dinner nearby"},
        )
        assert "event: reasoning_step" in response.text

    def test_stream_contains_message_frame(self, client: TestClient) -> None:
        response = client.post(
            "/v1/chat/stream",
            json={"user_id": "u1", "message": "dinner nearby"},
        )
        assert "event: message" in response.text
        assert "Here is my recommendation" in response.text

    def test_reasoning_step_frame_has_expected_shape(self, client: TestClient) -> None:
        """reasoning_step frames contain step, summary, source, tool_name fields."""
        import json

        response = client.post(
            "/v1/chat/stream",
            json={"user_id": "u1", "message": "dinner nearby"},
        )
        lines = response.text.splitlines()
        step_data: dict[str, Any] | None = None
        for i, line in enumerate(lines):
            if line.startswith("event: reasoning_step"):
                # look at the next data line
                for j in range(i + 1, min(i + 3, len(lines))):
                    if lines[j].startswith("data: "):
                        step_data = json.loads(lines[j][len("data: ") :])
                        break
                break

        assert step_data is not None
        assert "step" in step_data
        assert "summary" in step_data
        assert "source" in step_data

    def test_stream_message_frame_has_content_key(self, client: TestClient) -> None:
        import json

        response = client.post(
            "/v1/chat/stream",
            json={"user_id": "u1", "message": "dinner nearby"},
        )
        lines = response.text.splitlines()
        msg_data: dict[str, Any] | None = None
        for i, line in enumerate(lines):
            if line.startswith("event: message"):
                for j in range(i + 1, min(i + 3, len(lines))):
                    if lines[j].startswith("data: "):
                        msg_data = json.loads(lines[j][len("data: ") :])
                        break
                break

        assert msg_data is not None
        assert "content" in msg_data
        assert msg_data["content"] == "Here is my recommendation"


class TestChatStreamToolCallsUsed:
    """Verify SSE stream emits a done event with tool_calls_used."""

    def test_stream_contains_done_frame(self, client: TestClient) -> None:
        response = client.post(
            "/v1/chat/stream",
            json={"user_id": "u1", "message": "dinner nearby"},
        )
        assert "event: done" in response.text

    def test_done_frame_has_tool_calls_used(self, client: TestClient) -> None:
        import json

        response = client.post(
            "/v1/chat/stream",
            json={"user_id": "u1", "message": "dinner nearby"},
        )
        lines = response.text.splitlines()
        done_data: dict[str, Any] | None = None
        for i, line in enumerate(lines):
            if line.startswith("event: done"):
                for j in range(i + 1, min(i + 3, len(lines))):
                    if lines[j].startswith("data: "):
                        done_data = json.loads(lines[j][len("data: ") :])
                        break
                break

        assert done_data is not None
        assert "tool_calls_used" in done_data
        assert isinstance(done_data["tool_calls_used"], int)

    def test_done_frame_reflects_graph_tool_calls_used(self) -> None:
        """done event carries tool_calls_used from the final graph state."""
        import json

        from langchain_core.messages import AIMessage

        svc = _make_mock_service()
        graph = MagicMock()

        async def _stream_with_tool_calls(
            payload: Any, config: Any, stream_mode: Any = None
        ) -> AsyncGenerator[tuple[str, Any], None]:
            yield (
                "values",
                {
                    "messages": [AIMessage(content="Here you go")],
                    "tool_calls_used": 2,
                },
            )

        graph.astream = _stream_with_tool_calls

        from kebi.api.deps import get_agent_graph, get_chat_service

        app.dependency_overrides[get_chat_service] = lambda: svc
        app.dependency_overrides[get_agent_graph] = lambda: graph
        try:
            tc = TestClient(app)
            response = tc.post(
                "/v1/chat/stream",
                json={"user_id": "u1", "message": "dinner nearby"},
            )
            lines = response.text.splitlines()
            done_data = None
            for i, line in enumerate(lines):
                if line.startswith("event: done"):
                    for j in range(i + 1, min(i + 3, len(lines))):
                        if lines[j].startswith("data: "):
                            done_data = json.loads(lines[j][len("data: ") :])
                            break
                    break
            assert done_data is not None
            assert done_data["tool_calls_used"] == 2
        finally:
            app.dependency_overrides.pop(get_chat_service, None)
            app.dependency_overrides.pop(get_agent_graph, None)


class TestChatStreamDisabledAgent:
    """Verify /v1/chat/stream returns 400 when agent is disabled or graph is None."""

    def test_returns_400_when_agent_disabled(self) -> None:
        from unittest.mock import patch

        from kebi.core.config import EnvConfig

        disabled_env = MagicMock(spec=EnvConfig)
        disabled_env.AGENT_ENABLED = False
        app.dependency_overrides[get_chat_service] = lambda: _make_mock_service()
        app.dependency_overrides[get_agent_graph] = lambda: MagicMock()
        with patch("kebi.api.routes.chat.get_env", return_value=disabled_env):
            try:
                tc = TestClient(app)
                response = tc.post(
                    "/v1/chat/stream",
                    json={"user_id": "u1", "message": "test"},
                )
                assert response.status_code == 400
            finally:
                app.dependency_overrides.pop(get_chat_service, None)
                app.dependency_overrides.pop(get_agent_graph, None)

    def test_returns_400_when_graph_is_none(self) -> None:
        enabled_svc = _make_mock_service()
        app.dependency_overrides[get_chat_service] = lambda: enabled_svc
        app.dependency_overrides[get_agent_graph] = lambda: None
        try:
            tc = TestClient(app)
            response = tc.post(
                "/v1/chat/stream",
                json={"user_id": "u1", "message": "test"},
            )
            assert response.status_code == 400
        finally:
            app.dependency_overrides.pop(get_chat_service, None)
            app.dependency_overrides.pop(get_agent_graph, None)
