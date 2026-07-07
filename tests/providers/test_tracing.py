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
    span.end(output={"result": "ok"}, usage={"input": 1, "output": 1, "total": 2})
    client.capture_message(message="hello", level="info", metadata={"k": "v"})
    client.flush()


def _mock_observation_ctx(mock_observation):
    """Build a mock context manager that yields `mock_observation` on enter."""
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=mock_observation)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def test_langfuse_client_generation_delegates_to_sdk(monkeypatch):
    """`generation()` opens an `_as_current_` observation so children nest.

    PII scrubbing is on by default. This test asserts the verbatim
    delegation shape, so we disable scrubbing for it; a separate
    test below covers the scrubbed path.
    """
    monkeypatch.setenv("LANGFUSE_SCRUB_INPUT", "false")
    from kebi.core import config as cfg

    cfg._env = None  # type: ignore[attr-defined]

    mock_observation = MagicMock()
    cm = _mock_observation_ctx(mock_observation)
    mock_sdk = MagicMock()
    mock_sdk.start_as_current_observation.return_value = cm

    client = tracing_module._LangfuseTracingClient(mock_sdk)
    span = client.generation(
        name="my_op", input={"text": "hi"}, model="gpt-4o-mini", user_id="u1"
    )
    span.end(output={"count": 3})

    mock_sdk.start_as_current_observation.assert_called_once_with(
        as_type="generation",
        name="my_op",
        input={"text": "hi"},
        model="gpt-4o-mini",
        metadata={"user_id": "u1"},
    )
    cm.__enter__.assert_called_once()
    mock_observation.update.assert_called_once_with(
        level="DEFAULT", output={"count": 3}
    )
    cm.__exit__.assert_called_once()
    cfg._env = None  # type: ignore[attr-defined]


def test_langfuse_client_generation_scrubs_pii_keys_by_default():
    """When LANGFUSE_SCRUB_INPUT is on (production default), PII keys
    inside `input` are replaced by a `{redacted, len, sha8}` marker."""
    mock_observation = MagicMock()
    cm = _mock_observation_ctx(mock_observation)
    mock_sdk = MagicMock()
    mock_sdk.start_as_current_observation.return_value = cm

    client = tracing_module._LangfuseTracingClient(mock_sdk)
    client.generation(
        name="extractor",
        input={"message": "I am vegetarian", "count": 5},
        model="gpt-4o-mini",
    )

    call_input = mock_sdk.start_as_current_observation.call_args.kwargs["input"]
    assert call_input["count"] == 5  # non-PII passes through
    assert call_input["message"]["redacted"] is True
    assert call_input["message"]["len"] == len("I am vegetarian")


def test_langfuse_client_generation_propagates_metadata():
    """Caller metadata merges with user/session into the SDK metadata kwarg."""
    mock_sdk = MagicMock()
    mock_sdk.start_as_current_observation.return_value = _mock_observation_ctx(
        MagicMock()
    )
    client = tracing_module._LangfuseTracingClient(mock_sdk)

    client.generation(
        name="op",
        user_id="u1",
        session_id="s1",
        metadata={"feature": "agent", "tool": "find_saved"},
    )

    kwargs = mock_sdk.start_as_current_observation.call_args.kwargs
    assert kwargs["metadata"] == {
        "feature": "agent",
        "tool": "find_saved",
        "user_id": "u1",
        "session_id": "s1",
    }


def test_langfuse_span_end_forwards_usage_as_usage_details():
    """`end(usage=...)` reaches Langfuse as `usage_details=...` for v3 pricing."""
    mock_observation = MagicMock()
    cm = _mock_observation_ctx(mock_observation)
    mock_sdk = MagicMock()
    mock_sdk.start_as_current_observation.return_value = cm
    client = tracing_module._LangfuseTracingClient(mock_sdk)

    span = client.generation(name="op")
    span.end(usage={"input": 100, "output": 25, "total": 125})

    mock_observation.update.assert_called_once_with(
        level="DEFAULT", usage_details={"input": 100, "output": 25, "total": 125}
    )
    cm.__exit__.assert_called_once()


def test_langfuse_span_end_exits_context_manager_to_pop_otel_stack():
    """Critical for nesting: every generation must exit its context manager
    so the OTel contextvar stack returns to the parent observation. Without
    this, a sibling generation would land as the previous one's child rather
    than as another child of the chat_turn parent."""
    mock_observation = MagicMock()
    cm = _mock_observation_ctx(mock_observation)
    mock_sdk = MagicMock()
    mock_sdk.start_as_current_observation.return_value = cm
    client = tracing_module._LangfuseTracingClient(mock_sdk)

    span = client.generation(name="op")
    cm.__exit__.assert_not_called()
    span.end()
    cm.__exit__.assert_called_once_with(None, None, None)


async def test_langfuse_client_trace_sets_user_session_metadata_tags():
    """trace() opens a root span and stamps user/session/metadata/tags on it."""
    mock_root_ctx = MagicMock()
    mock_root_ctx.__enter__ = MagicMock(return_value=MagicMock())
    mock_root_ctx.__exit__ = MagicMock(return_value=False)
    mock_sdk = MagicMock()
    mock_sdk.start_as_current_observation.return_value = mock_root_ctx

    client = tracing_module._LangfuseTracingClient(mock_sdk)
    async with client.trace(
        name="chat_turn",
        user_id="u1",
        session_id="s1",
        metadata={"feature": "chat"},
        tags=["feature:chat"],
    ):
        pass

    mock_sdk.start_as_current_observation.assert_called_once_with(
        as_type="span", name="chat_turn"
    )
    mock_sdk.update_current_trace.assert_called_once_with(
        user_id="u1",
        session_id="s1",
        metadata={"feature": "chat"},
        tags=["feature:chat"],
    )


async def test_null_client_trace_is_a_no_op():
    """`_NullTracingClient.trace()` is an async context manager that does nothing."""
    client = tracing_module._NullTracingClient()
    async with client.trace(name="x", user_id="u1"):
        # ensure the body runs and the context manager exits cleanly
        pass


def test_langfuse_client_capture_message_delegates_to_sdk():
    """Events also use `_as_current_` so they nest under the active trace."""
    cm = _mock_observation_ctx(MagicMock())
    mock_sdk = MagicMock()
    mock_sdk.start_as_current_observation.return_value = cm

    client = tracing_module._LangfuseTracingClient(mock_sdk)
    client.capture_message(message="event handled", level="info", metadata={"id": "1"})

    mock_sdk.start_as_current_observation.assert_called_once_with(
        as_type="event",
        name="event handled",
        input={"level": "info", "id": "1"},
    )
    cm.__enter__.assert_called_once()
    cm.__exit__.assert_called_once()


def test_langfuse_client_flush_delegates_to_sdk():
    mock_sdk = MagicMock()
    client = tracing_module._LangfuseTracingClient(mock_sdk)
    client.flush()
    mock_sdk.flush.assert_called_once()
