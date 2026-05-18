"""Tests for SQLAlchemyTasteModelRepository.

Covers log_interaction accepting and persisting the optional metadata kwarg.
"""

from __future__ import annotations

from types import SimpleNamespace
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

    metadata = {"note": "anything"}

    await repo.log_interaction(
        user_id="user_abc",
        interaction_type=InteractionType.SAVE,
        place_core_id="pid-1",
        metadata=metadata,
    )

    session.add.assert_called_once()
    interaction = session.add.call_args[0][0]
    assert isinstance(interaction, Interaction)
    assert interaction.user_id == "user_abc"
    assert interaction.type == InteractionType.SAVE
    assert interaction.place_id == "pid-1"
    assert interaction.metadata_ == metadata
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_log_interaction_without_metadata_stores_null() -> None:
    factory, session = _mock_session_factory()
    repo = SQLAlchemyTasteModelRepository(factory)

    await repo.log_interaction(
        user_id="user_abc",
        interaction_type=InteractionType.SAVE,
        place_core_id="pid-1",
    )

    interaction = session.add.call_args[0][0]
    assert interaction.metadata_ is None


@pytest.mark.asyncio
async def test_get_interactions_returns_raw_rows_no_join() -> None:
    """get_interactions selects type + place_id only (no places JOIN)."""
    factory, session = _mock_session_factory()
    rows = [
        SimpleNamespace(type=InteractionType.SAVE, place_id="pv2-a"),
        SimpleNamespace(type="rejected", place_id=None),
    ]
    session.execute = AsyncMock(return_value=rows)
    repo = SQLAlchemyTasteModelRepository(factory)

    result = await repo.get_interactions("user_abc")

    # Enum coerced to its value; None place_core_id preserved. The DB
    # column stays `place_id`; RawInteraction exposes it as place_core_id.
    assert [(r.type, r.place_core_id) for r in result] == [
        ("save", "pv2-a"),
        ("rejected", None),
    ]
