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
from kebi.core.areas.keys import encode_area_id
from kebi.core.chat.service import ChatService
from kebi.core.config import AppConfig

# Area URIs carry the geo key encoded as one opaque segment (ADR-153).
_CANGGU_URI = f"kebi://area/{encode_area_id('id/badung/canggu')}"


def _make_mock_service() -> MagicMock:
    """Build a mock ChatService with async taste/memory helpers."""
    svc = MagicMock(spec=ChatService)
    svc._config = MagicMock(spec=AppConfig)
    svc._compose_taste_summary = AsyncMock(return_value="")
    # One read yields both the prose summary and the matchable values (ADR-142).
    svc.compose_taste = AsyncMock(return_value=("", []))
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
            step="agent.tool_decision",
            summary="responding directly",
            source="agent",
            visibility="user",
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
            json={"message": "dinner nearby"},
            headers={"Accept": "text/event-stream"},
        )
        assert response.status_code == 200

    def test_stream_content_type_is_event_stream(self, client: TestClient) -> None:
        response = client.post(
            "/v1/chat/stream",
            json={"message": "dinner nearby"},
        )
        assert "text/event-stream" in response.headers["content-type"]

    def test_stream_contains_reasoning_step_frame(self, client: TestClient) -> None:
        response = client.post(
            "/v1/chat/stream",
            json={"message": "dinner nearby"},
        )
        assert "event: reasoning_step" in response.text

    def test_stream_contains_message_frame(self, client: TestClient) -> None:
        response = client.post(
            "/v1/chat/stream",
            json={"message": "dinner nearby"},
        )
        assert "event: message" in response.text
        assert "Here is my recommendation" in response.text

    def test_reasoning_step_frame_has_expected_shape(self, client: TestClient) -> None:
        """reasoning_step frames contain step, title, summary, source fields."""
        import json

        response = client.post(
            "/v1/chat/stream",
            json={"message": "dinner nearby"},
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
        assert "title" in step_data
        assert "summary" in step_data
        assert "source" in step_data

    def test_stream_message_frame_has_content_key(self, client: TestClient) -> None:
        import json

        response = client.post(
            "/v1/chat/stream",
            json={"message": "dinner nearby"},
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


class TestChatStreamReasoningLifecycle:
    """Verify the active→done step lifecycle on the SSE stream (ADR-102)."""

    @staticmethod
    def _reasoning_frames(text: str) -> list[dict[str, Any]]:
        import json

        lines = text.splitlines()
        frames: list[dict[str, Any]] = []
        for i, line in enumerate(lines):
            if line.startswith("event: reasoning_step"):
                for j in range(i + 1, min(i + 3, len(lines))):
                    if lines[j].startswith("data: "):
                        frames.append(json.loads(lines[j][len("data: ") :]))
                        break
        return frames

    @pytest.fixture
    def lifecycle_graph(self) -> MagicMock:
        """A graph that streams an `active` then a `done` frame for one step."""
        graph = MagicMock()

        async def _astream(
            payload: Any, config: Any, stream_mode: Any = None
        ) -> AsyncGenerator[tuple[str, Any], None]:
            active = ReasoningStep(
                step="find_saved",
                title="searched your saved spots",
                summary=None,
                source="agent",
                id="find_saved#0",
                status="active",
            )
            done = ReasoningStep(
                step="find_saved.summary",
                title="searched your saved spots",
                summary="2 spots — A, B",
                source="agent",
                duration_ms=420.0,
                id="find_saved#0",
                status="done",
            )
            yield ("custom", active.model_dump(mode="json"))
            yield ("custom", done.model_dump(mode="json"))

            from langchain_core.messages import AIMessage

            yield (
                "values",
                {"messages": [AIMessage(content="here you go")], "tool_calls_used": 1},
            )

        graph.astream = _astream
        return graph

    def test_each_done_frame_has_a_prior_active_with_same_id(
        self, mock_service: MagicMock, lifecycle_graph: MagicMock
    ) -> None:
        app.dependency_overrides[get_chat_service] = lambda: mock_service
        app.dependency_overrides[get_agent_graph] = lambda: lifecycle_graph
        try:
            response = TestClient(app).post(
                "/v1/chat/stream", json={"message": "saved bars"}
            )
        finally:
            app.dependency_overrides.pop(get_chat_service, None)
            app.dependency_overrides.pop(get_agent_graph, None)

        frames = self._reasoning_frames(response.text)
        assert len(frames) == 2

        active = next(f for f in frames if f["status"] == "active")
        done = next(f for f in frames if f["status"] == "done")
        # Same id pairs the two frames; the active arrived first.
        assert active["id"] == done["id"]
        assert frames.index(active) < frames.index(done)
        # Active carries the title but no summary/duration (frontend shows a
        # skeleton with the action line); done fills both in (ADR-103).
        assert active["title"] == "searched your saved spots"
        assert active["summary"] is None
        assert active["duration_ms"] is None
        assert done["title"] == "searched your saved spots"
        assert done["summary"] == "2 spots — A, B"
        assert done["duration_ms"] == 420.0


class TestChatStreamToolCallsUsed:
    """Verify SSE stream emits a done event with tool_calls_used."""

    def test_stream_contains_done_frame(self, client: TestClient) -> None:
        response = client.post(
            "/v1/chat/stream",
            json={"message": "dinner nearby"},
        )
        assert "event: done" in response.text

    def test_done_frame_has_tool_calls_used(self, client: TestClient) -> None:
        import json

        response = client.post(
            "/v1/chat/stream",
            json={"message": "dinner nearby"},
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
                json={"message": "dinner nearby"},
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


class TestChatStreamDispatchesIntentSignal:
    """The stream path must pass `surfaced_places` on TurnCompleted so the
    recall list is populated (ADR-110) — regression for the bug where the
    stream endpoint dispatched without it and no intent was ever recorded."""

    @staticmethod
    def _graph(tool_results: list[dict[str, Any]] | None) -> MagicMock:
        from langchain_core.messages import AIMessage

        graph = MagicMock()

        async def _astream(
            payload: Any, config: Any, stream_mode: Any = None
        ) -> AsyncGenerator[tuple[str, Any], None]:
            values: dict[str, Any] = {
                "messages": [AIMessage(content="here you go")],
                "tool_calls_used": 1 if tool_results else 0,
            }
            if tool_results is not None:
                values["tool_results"] = tool_results
            yield ("values", values)

        graph.astream = _astream
        return graph

    def _dispatched_event(self, svc: MagicMock, graph: MagicMock) -> Any:
        app.dependency_overrides[get_chat_service] = lambda: svc
        app.dependency_overrides[get_agent_graph] = lambda: graph
        try:
            TestClient(app).post("/v1/chat/stream", json={"message": "dinner nearby"})
        finally:
            app.dependency_overrides.pop(get_chat_service, None)
            app.dependency_overrides.pop(get_agent_graph, None)
        return svc._dispatcher.dispatch.await_args.args[0]

    def test_surfaced_true_when_tool_results_present(
        self, mock_service: MagicMock
    ) -> None:
        graph = self._graph([{"tool": "suggest_places", "payload": {}}])
        event = self._dispatched_event(mock_service, graph)
        assert event.surfaced_places is True
        assert event.user_message == "dinner nearby"

    def test_surfaced_false_when_no_tool_results(self, mock_service: MagicMock) -> None:
        graph = self._graph(None)
        event = self._dispatched_event(mock_service, graph)
        assert event.surfaced_places is False


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
                    json={"message": "test"},
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
                json={"message": "test"},
            )
            assert response.status_code == 400
        finally:
            app.dependency_overrides.pop(get_chat_service, None)
            app.dependency_overrides.pop(get_agent_graph, None)


class TestChatStreamEntityLinks:
    """The stream's render contract: one `message` frame carrying linkified
    text plus `entities`, and no `tool_result` frames at all (ADR-136)."""

    @staticmethod
    def _graph() -> MagicMock:
        from langchain_core.messages import AIMessage

        graph = MagicMock()

        async def _astream(
            payload: Any, config: Any, stream_mode: Any = None
        ) -> AsyncGenerator[tuple[str, Any], None]:
            yield (
                "values",
                {
                    "messages": [],
                    "working_location": {
                        "country": "Indonesia",
                        "country_code": "id",
                        "city": "Badung",
                        "neighborhood": "Canggu",
                        "lat": -8.65,
                        "lng": 115.13,
                    },
                    "tool_results": [
                        {
                            "tool": "find_saved",
                            "tool_call_id": "call-1",
                            "payload": {
                                "recommendation_id": "rec-99",
                                "candidates": [
                                    {
                                        "place": {"id": "p1", "place_name": "Luigis"},
                                        "source": "saved",
                                    }
                                ],
                            },
                        }
                    ],
                },
            )
            yield (
                "values",
                {
                    "messages": [
                        AIMessage(content="tonight is Luigis night in Canggu")
                    ],
                    "tool_calls_used": 1,
                    "tool_results": [],
                },
            )

        graph.astream = _astream
        return graph

    def _message_frame(self, mock_service: MagicMock) -> dict[str, Any]:
        app.dependency_overrides[get_chat_service] = lambda: mock_service
        app.dependency_overrides[get_agent_graph] = lambda: self._graph()
        try:
            response = TestClient(app).post(
                "/v1/chat/stream", json={"message": "where tonight"}
            )
        finally:
            app.dependency_overrides.pop(get_chat_service, None)
            app.dependency_overrides.pop(get_agent_graph, None)
        import json

        self._raw = response.text
        lines = response.text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("event: message"):
                return json.loads(lines[i + 1][len("data: ") :])
        raise AssertionError("no message frame in stream")

    def test_message_content_is_linkified(self, mock_service: MagicMock) -> None:
        frame = self._message_frame(mock_service)
        assert frame["content"] == (
            f"tonight is [Luigis](kebi://venue/p1) night in [Canggu]({_CANGGU_URI})"
        )

    def test_message_frame_carries_entities(self, mock_service: MagicMock) -> None:
        frame = self._message_frame(mock_service)
        assert [(e["kind"], e["key"]) for e in frame["entities"]] == [
            ("venue", "p1"),
            ("area", "id/badung/canggu"),
        ]

    def test_no_tool_result_frames_are_emitted(self, mock_service: MagicMock) -> None:
        self._message_frame(mock_service)
        assert "event: tool_result" not in self._raw


class TestChatStreamMessageDeltas:
    """`message_delta` token streaming (ADR-158).

    The route subscribes to the orchestrator token stream: answer prose
    streams as plain-text deltas, narration before tool calls is
    suppressed, other nodes' LLM chunks are ignored, and the terminal
    `message` frame stays authoritative.
    """

    _ANSWER = (
        "tonight is Luigis night — the counter seats are the move, and if "
        "you get there before seven you will beat the queue that forms "
        "once the sunset crowd rolls off the beach and floods the lane."
    )

    def _client_for(self, graph: MagicMock, mock_service: MagicMock) -> TestClient:
        app.dependency_overrides[get_chat_service] = lambda: mock_service
        app.dependency_overrides[get_agent_graph] = lambda: graph
        return TestClient(app)

    def _chunk(self, text: str, *, cid: str = "m1", tools: bool = False) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            id=cid, content=text, tool_call_chunks=[{"name": "x"}] if tools else []
        )

    def _graph(self, message_chunks: list[tuple[Any, dict[str, Any]]]) -> MagicMock:
        graph = MagicMock()
        answer = self._ANSWER

        async def _astream(
            payload: Any, config: Any, stream_mode: Any = None
        ) -> AsyncGenerator[tuple[str, Any], None]:
            for pair in message_chunks:
                yield ("messages", pair)
            from langchain_core.messages import AIMessage

            yield (
                "values",
                {"messages": [AIMessage(content=answer)], "tool_calls_used": 0},
            )

        graph.astream = _astream
        return graph

    def _frames(self, raw: str, event: str) -> list[dict[str, Any]]:
        import json as _json

        lines = raw.splitlines()
        return [
            _json.loads(lines[i + 1][len("data: ") :])
            for i, line in enumerate(lines)
            if line == f"event: {event}"
        ]

    def test_answer_tokens_stream_as_plain_deltas(
        self, mock_service: MagicMock
    ) -> None:
        half = len(self._ANSWER) // 2
        graph = self._graph(
            [
                (self._chunk(self._ANSWER[:half]), {"langgraph_node": "agent"}),
                (
                    self._chunk(self._ANSWER[half:], cid="m1"),
                    {"langgraph_node": "agent"},
                ),
            ]
        )
        client = self._client_for(graph, mock_service)
        response = client.post("/v1/chat/stream", json={"message": "dinner"})
        app.dependency_overrides.clear()
        deltas = self._frames(response.text, "message_delta")
        assert deltas, "expected message_delta frames"
        streamed = "".join(d["text"] for d in deltas)
        # Plain prose only — links never stream (they ride the final frame).
        assert "kebi://" not in streamed
        # The invariant that makes the swap invisible: the streamed text is
        # byte-identical to the terminal frame's content (both pass through
        # normalize_voice; linkify is a no-op here with no tool results).
        messages = self._frames(response.text, "message")
        assert messages and messages[0]["content"] == streamed
        assert "counter seats are the move" in streamed

    def test_narration_before_tool_call_is_suppressed(
        self, mock_service: MagicMock
    ) -> None:
        graph = self._graph(
            [
                (
                    self._chunk("checking your saved spots in haifa"),
                    {"langgraph_node": "agent"},
                ),
                (self._chunk("", tools=True), {"langgraph_node": "agent"}),
            ]
        )
        client = self._client_for(graph, mock_service)
        response = client.post("/v1/chat/stream", json={"message": "dinner"})
        app.dependency_overrides.clear()
        assert self._frames(response.text, "message_delta") == []

    def test_non_agent_node_chunks_are_ignored(self, mock_service: MagicMock) -> None:
        graph = self._graph(
            [(self._chunk(self._ANSWER), {"langgraph_node": "resolve_location"})]
        )
        client = self._client_for(graph, mock_service)
        response = client.post("/v1/chat/stream", json={"message": "dinner"})
        app.dependency_overrides.clear()
        assert self._frames(response.text, "message_delta") == []


class TestChatStreamReasoningDeltas:
    """`reasoning_delta` live narration + promote flag (ADR-159)."""

    def _run(self, chunks: list[tuple[str, Any]], mock_service: MagicMock) -> str:
        graph = MagicMock()

        async def _astream(
            payload: Any, config: Any, stream_mode: Any = None
        ) -> AsyncGenerator[tuple[str, Any], None]:
            for pair in chunks:
                yield pair
            from langchain_core.messages import AIMessage

            yield (
                "values",
                {"messages": [AIMessage(content="final text")], "tool_calls_used": 1},
            )

        graph.astream = _astream
        app.dependency_overrides[get_chat_service] = lambda: mock_service
        app.dependency_overrides[get_agent_graph] = lambda: graph
        client = TestClient(app)
        response = client.post("/v1/chat/stream", json={"message": "dinner"})
        app.dependency_overrides.clear()
        return response.text

    @staticmethod
    def _frames(raw: str, event: str) -> list[dict[str, Any]]:
        import json as _json

        lines = raw.splitlines()
        return [
            _json.loads(lines[i + 1][len("data: ") :])
            for i, line in enumerate(lines)
            if line == f"event: {event}"
        ]

    @staticmethod
    def _chunk(text: str, *, tools: bool = False) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            id="m1", content=text, tool_call_chunks=[{"name": "x"}] if tools else []
        )

    def test_narration_types_into_the_active_thinking_step(
        self, mock_service: MagicMock
    ) -> None:
        active = ReasoningStep(
            step="agent.tool_decision",
            title="thinking",
            summary=None,
            source="agent",
            id="agent.tool_decision#0",
            status="active",
        )
        raw = self._run(
            [
                ("custom", active.model_dump(mode="json")),
                (
                    "messages",
                    (
                        self._chunk("checking what you've saved in canggu"),
                        {"langgraph_node": "agent"},
                    ),
                ),
                (
                    "messages",
                    (self._chunk("", tools=True), {"langgraph_node": "agent"}),
                ),
            ],
            mock_service,
        )
        deltas = self._frames(raw, "reasoning_delta")
        assert deltas, "expected reasoning_delta frames"
        assert all(d["id"] == "agent.tool_decision#0" for d in deltas)
        assert "".join(d["text"] for d in deltas) == (
            "checking what you've saved in canggu"
        )
        # Narration never leaks to the answer lane.
        assert self._frames(raw, "message_delta") == []

    def test_first_answer_delta_carries_promote(self, mock_service: MagicMock) -> None:
        raw = self._run(
            [("messages", (self._chunk("final text"), {"langgraph_node": "agent"}))],
            mock_service,
        )
        deltas = self._frames(raw, "message_delta")
        assert deltas and deltas[0].get("promote") is True
        assert deltas[0]["text"] == "final text"

    def test_narration_without_active_step_is_dropped(
        self, mock_service: MagicMock
    ) -> None:
        raw = self._run(
            [
                (
                    "messages",
                    (self._chunk("orphan narration"), {"langgraph_node": "agent"}),
                ),
                (
                    "messages",
                    (self._chunk("", tools=True), {"langgraph_node": "agent"}),
                ),
            ],
            mock_service,
        )
        assert self._frames(raw, "reasoning_delta") == []
        assert self._frames(raw, "message_delta") == []
