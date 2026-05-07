"""Unit tests for UserMemoryRepository implementations."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.core.memory.repository import (
    NullUserMemoryRepository,
    SQLAlchemyUserMemoryRepository,
)


class TestNullUserMemoryRepository:
    """Tests for NullUserMemoryRepository (no-op implementation)."""

    @pytest.mark.asyncio
    async def test_save_noop(self) -> None:
        """NullUserMemoryRepository.save() is a no-op."""
        repo = NullUserMemoryRepository()
        # Should not raise
        await repo.save(
            user_id="user-1",
            memory="I use a wheelchair",
            source="stated",
            confidence=0.9,
        )

    @pytest.mark.asyncio
    async def test_load_returns_empty_list(self) -> None:
        """NullUserMemoryRepository.load() always returns []."""
        repo = NullUserMemoryRepository()
        result = await repo.load("user-1")
        assert result == []


class TestSQLAlchemyUserMemoryRepository:
    """Tests for SQLAlchemyUserMemoryRepository (SQLAlchemy async impl)."""

    @pytest.fixture
    def mock_session(self) -> MagicMock:
        """Create a mock AsyncSession with execute/commit as AsyncMocks."""
        session = MagicMock()
        session.execute = AsyncMock()
        session.commit = AsyncMock()
        return session

    @pytest.fixture
    def mock_session_factory(self, mock_session: MagicMock) -> MagicMock:
        """Mimic async_sessionmaker: calling it returns an async context manager
        that yields ``mock_session``."""
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=None)
        factory = MagicMock(return_value=ctx)
        return factory

    @pytest.fixture
    def repo(self, mock_session_factory: MagicMock) -> SQLAlchemyUserMemoryRepository:
        """Create repository instance with mocked session factory."""
        return SQLAlchemyUserMemoryRepository(mock_session_factory)

    @pytest.mark.asyncio
    async def test_save_calls_execute(
        self, repo: SQLAlchemyUserMemoryRepository, mock_session: MagicMock
    ) -> None:
        """save() calls session.execute() and commit() with insert statement."""
        await repo.save(
            user_id="user-1",
            memory="I use a wheelchair",
            source="stated",
            confidence=0.9,
        )

        mock_session.execute.assert_called_once()
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_load_success(
        self, repo: SQLAlchemyUserMemoryRepository, mock_session: MagicMock
    ) -> None:
        """load() returns list of strings from database."""
        mock_result = MagicMock()
        mock_result.all.return_value = [("I use a wheelchair",), ("I'm vegetarian",)]
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.load("user-1")

        assert result == ["I use a wheelchair", "I'm vegetarian"]

    @pytest.mark.asyncio
    async def test_load_catches_exception_returns_empty_list(
        self, repo: SQLAlchemyUserMemoryRepository, mock_session: MagicMock
    ) -> None:
        """load() catches exceptions and returns []."""
        mock_session.execute = AsyncMock(side_effect=Exception("DB error"))

        result = await repo.load("user-1")

        assert result == []

    @pytest.mark.asyncio
    async def test_load_empty_result(
        self, repo: SQLAlchemyUserMemoryRepository, mock_session: MagicMock
    ) -> None:
        """load() returns [] when database has no results."""
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.load("user-1")

        assert result == []
