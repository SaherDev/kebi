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
