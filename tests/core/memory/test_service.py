"""Unit tests for UserMemoryService."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.core.config import MemoryConfidenceConfig
from kebi.core.memory.schemas import PersonalFact
from kebi.core.memory.service import UserMemoryService


class TestUserMemoryService:
    """Tests for UserMemoryService."""

    @pytest.fixture
    def mock_repo(self) -> MagicMock:
        """Create a mock UserMemoryRepository."""
        return MagicMock()

    @pytest.fixture
    def service(self, mock_repo: MagicMock) -> UserMemoryService:
        """Create UserMemoryService with mock repo."""
        return UserMemoryService(repo=mock_repo)

    @pytest.mark.asyncio
    async def test_save_facts_empty_list_skips_repo(
        self, service: UserMemoryService, mock_repo: MagicMock
    ) -> None:
        """save_facts() with empty list does not call repo."""
        config = MemoryConfidenceConfig(stated=0.9, inferred=0.6)
        await service.save_facts(user_id="user-1", facts=[], confidence_config=config)

        mock_repo.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_save_facts_assigns_stated_confidence(
        self, service: UserMemoryService, mock_repo: MagicMock
    ) -> None:
        """save_facts() assigns stated confidence from config."""
        mock_repo.save = AsyncMock()
        config = MemoryConfidenceConfig(stated=0.95, inferred=0.5)
        fact = PersonalFact(text="I'm vegan", source="stated")

        await service.save_facts(
            user_id="user-1", facts=[fact], confidence_config=config
        )

        mock_repo.save.assert_called_once()
        call_args = mock_repo.save.call_args
        assert call_args[1]["confidence"] == 0.95

    @pytest.mark.asyncio
    async def test_save_facts_assigns_inferred_confidence(
        self, service: UserMemoryService, mock_repo: MagicMock
    ) -> None:
        """save_facts() assigns inferred confidence from config."""
        mock_repo.save = AsyncMock()
        config = MemoryConfidenceConfig(stated=0.9, inferred=0.65)
        fact = PersonalFact(text="Seems like they want spicy food", source="inferred")

        await service.save_facts(
            user_id="user-1", facts=[fact], confidence_config=config
        )

        mock_repo.save.assert_called_once()
        call_args = mock_repo.save.call_args
        assert call_args[1]["confidence"] == 0.65

    @pytest.mark.asyncio
    async def test_save_facts_multiple_facts(
        self, service: UserMemoryService, mock_repo: MagicMock
    ) -> None:
        """save_facts() persists multiple facts."""
        mock_repo.save = AsyncMock()
        config = MemoryConfidenceConfig(stated=0.9, inferred=0.6)
        facts = [
            PersonalFact(text="I'm vegetarian", source="stated"),
            PersonalFact(text="I dislike spicy", source="inferred"),
        ]

        await service.save_facts(
            user_id="user-1", facts=facts, confidence_config=config
        )

        assert mock_repo.save.call_count == 2

    @pytest.mark.asyncio
    async def test_save_facts_passes_correct_fields(
        self, service: UserMemoryService, mock_repo: MagicMock
    ) -> None:
        """save_facts() passes user_id, memory text, source, and confidence to repo."""
        mock_repo.save = AsyncMock()
        config = MemoryConfidenceConfig(stated=0.9, inferred=0.6)
        fact = PersonalFact(text="I use a wheelchair", source="stated")

        await service.save_facts(
            user_id="user-1", facts=[fact], confidence_config=config
        )

        call_args = mock_repo.save.call_args
        assert call_args[1]["user_id"] == "user-1"
        assert call_args[1]["memory"] == "I use a wheelchair"
        assert call_args[1]["source"] == "stated"
        assert call_args[1]["confidence"] == 0.9

    @pytest.mark.asyncio
    async def test_load_memories_success(
        self, service: UserMemoryService, mock_repo: MagicMock
    ) -> None:
        """load_memories() returns strings from repo."""
        mock_repo.load = AsyncMock(return_value=["I'm vegetarian", "I hate seafood"])

        result = await service.load_memories("user-1")

        assert result == ["I'm vegetarian", "I hate seafood"]
        mock_repo.load.assert_called_once_with("user-1")

    @pytest.mark.asyncio
    async def test_load_memories_empty_list(
        self, service: UserMemoryService, mock_repo: MagicMock
    ) -> None:
        """load_memories() returns [] when repo returns []."""
        mock_repo.load = AsyncMock(return_value=[])

        result = await service.load_memories("user-1")

        assert result == []

    @pytest.mark.asyncio
    async def test_load_memories_repo_raises_returns_empty_list(
        self, service: UserMemoryService, mock_repo: MagicMock
    ) -> None:
        """load_memories() returns [] when repo raises exception."""
        mock_repo.load = AsyncMock(side_effect=Exception("DB error"))

        result = await service.load_memories("user-1")

        assert result == []

    @pytest.mark.asyncio
    async def test_load_memories_never_raises(
        self, service: UserMemoryService, mock_repo: MagicMock
    ) -> None:
        """load_memories() never raises, always returns list or []."""
        mock_repo.load = AsyncMock(side_effect=RuntimeError("Connection lost"))

        # Should not raise
        result = await service.load_memories("user-1")
        assert result == []
        assert isinstance(result, list)
