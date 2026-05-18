"""Route tests for POST /v1/chat endpoint."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from kebi.api.deps import get_chat_service
from kebi.api.main import app
from kebi.api.schemas.chat import ChatResponse
from kebi.core.chat.service import ChatService


@pytest.fixture
def mock_chat_service() -> AsyncMock:
    """Mock ChatService for dependency injection."""
    return AsyncMock(spec=ChatService)


@pytest.fixture
def client(mock_chat_service: AsyncMock) -> TestClient:
    """FastAPI test client with mocked ChatService."""
    app.dependency_overrides[get_chat_service] = lambda: mock_chat_service
    yield TestClient(app)
    app.dependency_overrides.pop(get_chat_service, None)


class TestChatRouteHappyPath:
    """Verify POST /v1/chat returns 200. ADR-075: the agent is a
    zero-tool Q&A surface — only `type="agent"` (or `"error"`)."""

    def test_agent_intent_returns_200_with_type(
        self, client: TestClient, mock_chat_service: AsyncMock
    ) -> None:
        """Response shape for agent response (ADR-065/ADR-075)."""
        mock_chat_service.run.return_value = ChatResponse(
            type="agent",
            message="Tipping is not expected in Japan.",
            data={"reasoning_steps": []},
        )

        response = client.post(
            "/v1/chat",
            json={"user_id": "user_1", "message": "is tipping expected in Japan?"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["type"] == "agent"
        assert data["message"] == "Tipping is not expected in Japan."

    def test_chat_with_location_passes_through(
        self, client: TestClient, mock_chat_service: AsyncMock
    ) -> None:
        """POST /v1/chat accepts optional location field."""
        mock_chat_service.run.return_value = ChatResponse(
            type="agent",
            message="Magdeburg is known for its cathedral.",
            data={"reasoning_steps": []},
        )

        response = client.post(
            "/v1/chat",
            json={
                "user_id": "user_1",
                "message": "what's this city known for",
                "location": {"lat": 13.7563, "lng": 100.5018},
            },
        )

        assert response.status_code == 200
        mock_chat_service.run.assert_called_once()


class TestChatRouteToolCallsUsed:
    """Verify POST /v1/chat response includes tool_calls_used."""

    def test_agent_response_includes_tool_calls_used(
        self, client: TestClient, mock_chat_service: AsyncMock
    ) -> None:
        mock_chat_service.run.return_value = ChatResponse(
            type="agent",
            message="Here are some ramen spots.",
            data={"reasoning_steps": []},
            tool_calls_used=2,
        )

        response = client.post(
            "/v1/chat",
            json={"user_id": "user_1", "message": "find me ramen"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["tool_calls_used"] == 2

    def test_tool_calls_used_defaults_to_zero(
        self, client: TestClient, mock_chat_service: AsyncMock
    ) -> None:
        mock_chat_service.run.return_value = ChatResponse(
            type="agent",
            message="No tools needed.",
            data={"reasoning_steps": []},
        )

        response = client.post(
            "/v1/chat",
            json={"user_id": "user_1", "message": "hello"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["tool_calls_used"] == 0


class TestChatRouteValidation:
    """Verify request validation for POST /v1/chat."""

    def test_missing_user_id_returns_422(self) -> None:
        """Missing user_id field is rejected with 422."""
        test_client = TestClient(app)
        response = test_client.post(
            "/v1/chat",
            json={"message": "cheap dinner"},
        )
        assert response.status_code == 422

    def test_missing_message_returns_422(self) -> None:
        """Missing message field is rejected with 422."""
        test_client = TestClient(app)
        response = test_client.post(
            "/v1/chat",
            json={"user_id": "user_1"},
        )
        assert response.status_code == 422
