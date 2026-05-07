"""Tests for SQLAlchemyTasteModelRepository feature-023 additions.

Covers:
- log_interaction accepting and persisting metadata kwarg.
- merge_chip_statuses replacing the chips JSONB array via upsert.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.db.models import Interaction, InteractionType
from kebi.db.repositories.taste_model_repository import (
    SQLAlchemyTasteModelRepository,
)


def _mock_session_factory() -> tuple[MagicMock, AsyncMock]:
    """Return (factory, session) where factory() returns an async-context session.

    Supports `async with self._session_factory() as session:` pattern.
    """
    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    ctx = AsyncMock()
    ctx.__aenter__.return_value = session
    ctx.__aexit__.return_value = None

    factory = MagicMock(return_value=ctx)
    return factory, session


@pytest.mark.asyncio
async def test_log_interaction_persists_metadata() -> None:
    factory, session = _mock_session_factory()
    repo = SQLAlchemyTasteModelRepository(factory)

    metadata = {"round": "round_1", "chips": [{"label": "foo"}]}

    await repo.log_interaction(
        user_id="user_abc",
        interaction_type=InteractionType.CHIP_CONFIRM,
        place_id=None,
        metadata=metadata,
    )

    session.add.assert_called_once()
    interaction = session.add.call_args[0][0]
    assert isinstance(interaction, Interaction)
    assert interaction.user_id == "user_abc"
    assert interaction.type == InteractionType.CHIP_CONFIRM
    assert interaction.place_id is None
    assert interaction.metadata_ == metadata
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_log_interaction_without_metadata_stores_null() -> None:
    factory, session = _mock_session_factory()
    repo = SQLAlchemyTasteModelRepository(factory)

    await repo.log_interaction(
        user_id="user_abc",
        interaction_type=InteractionType.SAVE,
        place_id="pid-1",
    )

    interaction = session.add.call_args[0][0]
    assert interaction.metadata_ is None


@pytest.mark.asyncio
async def test_merge_chip_statuses_executes_upsert() -> None:
    factory, session = _mock_session_factory()
    repo = SQLAlchemyTasteModelRepository(factory)

    updated_chips = [
        {
            "label": "Ramen lover",
            "source_field": "attributes.cuisine",
            "source_value": "ramen",
            "signal_count": 5,
            "status": "confirmed",
            "selection_round": "round_1",
        }
    ]

    await repo.merge_chip_statuses(user_id="user_abc", updated_chips=updated_chips)

    session.execute.assert_awaited_once()
    session.commit.assert_awaited_once()
