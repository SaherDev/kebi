"""Unit tests for the TracingClient abstraction."""

from unittest.mock import MagicMock, patch

import pytest

import kebi.providers.tracing as tracing_module
from kebi.providers.tracing import (
    TracingClient,
    TracingSpan,
    get_tracing_client,
)


@pytest.fixture(autouse=True)
def reset_tracing_cache():
    """Reset the module-level singleton between tests."""
    original = tracing_module._client
    tracing_module._client = tracing_module._UNSET
    yield
    tracing_module._client = original


def test_get_tracing_client_returns_langfuse_adapter_when_configured():
    mock_lf = MagicMock()
    mock_lf.auth_check.return_value = None
    mock_langfuse_module = MagicMock()
    mock_langfuse_module.Langfuse.return_value = mock_lf

    with patch.dict("sys.modules", {"langfuse": mock_langfuse_module}):
        client = get_tracing_client()

    assert isinstance(client, tracing_module._LangfuseTracingClient)


def test_get_tracing_client_returns_null_adapter_when_langfuse_missing():
    with patch.dict("sys.modules", {"langfuse": None}):
        client = get_tracing_client()

    assert isinstance(client, tracing_module._NullTracingClient)


def test_get_tracing_client_returns_null_adapter_when_auth_fails():
    mock_lf = MagicMock()
    mock_lf.auth_check.side_effect = Exception("invalid credentials")
    mock_langfuse_module = MagicMock()
    mock_langfuse_module.Langfuse.return_value = mock_lf

    with patch.dict("sys.modules", {"langfuse": mock_langfuse_module}):
        client = get_tracing_client()

    assert isinstance(client, tracing_module._NullTracingClient)


def test_get_tracing_client_is_cached():
    with patch.dict("sys.modules", {"langfuse": None}):
        c1 = get_tracing_client()
        c2 = get_tracing_client()

    assert c1 is c2


def test_null_client_satisfies_protocol():
    client = tracing_module._NullTracingClient()
    assert isinstance(client, TracingClient)
    span = client.generation(name="test", input={"x": 1}, model="gpt-4o-mini")
    assert isinstance(span, TracingSpan)
    span.end(output={"result": "ok"})
    client.capture_message(message="hello", level="info", metadata={"k": "v"})
    client.flush()


def test_langfuse_client_generation_delegates_to_sdk():
    mock_gen = MagicMock()
    mock_sdk = MagicMock()
    mock_sdk.start_observation.return_value = mock_gen

    client = tracing_module._LangfuseTracingClient(mock_sdk)
    span = client.generation(
        name="my_op", input={"text": "hi"}, model="gpt-4o-mini", user_id="u1"
    )
    span.end(output={"count": 3})

    mock_sdk.start_observation.assert_called_once_with(
        as_type="generation",
        name="my_op",
        input={"text": "hi"},
        model="gpt-4o-mini",
        metadata={"user_id": "u1"},
    )
    mock_gen.update.assert_called_once_with(level="DEFAULT", output={"count": 3})
    mock_gen.end.assert_called_once()


def test_langfuse_client_capture_message_delegates_to_sdk():
    mock_obs = MagicMock()
    mock_sdk = MagicMock()
    mock_sdk.start_observation.return_value = mock_obs

    client = tracing_module._LangfuseTracingClient(mock_sdk)
    client.capture_message(message="event handled", level="info", metadata={"id": "1"})

    mock_sdk.start_observation.assert_called_once_with(
        as_type="event",
        name="event handled",
        input={"level": "info", "id": "1"},
    )
    mock_obs.end.assert_called_once()


def test_langfuse_client_flush_delegates_to_sdk():
    mock_sdk = MagicMock()
    client = tracing_module._LangfuseTracingClient(mock_sdk)
    client.flush()
    mock_sdk.flush.assert_called_once()
