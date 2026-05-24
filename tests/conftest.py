"""Pytest configuration and fixtures."""

import os
from unittest.mock import AsyncMock

import pytest

from kebi.api.main import app


@pytest.fixture(scope="session", autouse=True)
def setup_test_env() -> None:
    """Set up environment variables for testing."""
    # Set dummy API keys for testing (fallback for code that reads from env)
    os.environ.setdefault("GOOGLE_PLACES_API_KEY", "test-key-dummy")
    os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
    os.environ.setdefault("VOYAGE_API_KEY", "voyage-test-dummy")
    # Set dummy database URL to avoid connection attempts
    os.environ.setdefault(
        "DATABASE_URL", "postgresql+asyncpg://user:password@localhost/testdb"
    )


@pytest.fixture(scope="session", autouse=True)
def disable_langfuse_in_tests() -> None:
    """Force the no-op tracing client for the whole test session.

    Without this, tests that exercise instrumented code paths ship spans
    to the developer's real Langfuse project whenever `LANGFUSE_*` env
    vars happen to be loaded via `.env`. Forcing the no-op client at
    session start runs before any test imports the instrumented modules.
    Tests that DO want to verify the Langfuse adapter shape (in
    `test_tracing.py`) use their own function-scoped `reset_tracing_cache`
    fixture to clear and re-resolve the singleton with mocked imports.
    """
    from kebi.providers import tracing

    tracing._client = tracing._NullTracingClient()


@pytest.fixture
def mock_session() -> AsyncMock:
    """Provide a mocked AsyncSession for dependency injection."""
    from unittest.mock import MagicMock

    session = AsyncMock()
    # Async methods
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    # Synchronous method (not async)
    session.add = MagicMock()
    return session


@pytest.fixture(autouse=True)
def override_session_dependency(mock_session: AsyncMock) -> None:
    """Override the get_session dependency for all tests."""
    from kebi.api import deps

    app.dependency_overrides[deps.get_session] = lambda: mock_session


@pytest.fixture(autouse=True)
def clear_provider_factory_caches() -> None:
    """Reset @functools.cache on provider factories between tests.

    Keys are call args (e.g. `role`), not `get_config()` / `get_env()`
    outputs — so a test that patches config could otherwise see a stale
    instance cached by an earlier test using the same role.
    """
    from kebi.providers.embeddings import get_embedder
    from kebi.providers.http_client import get_shared_http_client
    from kebi.providers.llm import get_instructor_client, get_langchain_chat_model

    get_embedder.cache_clear()
    get_instructor_client.cache_clear()
    get_langchain_chat_model.cache_clear()
    get_shared_http_client.cache_clear()
