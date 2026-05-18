"""Tests for TasteModelService (ADR-058).

Tests handle_signal, _run_regen guards, and the happy path.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from kebi.core.taste.schemas import (
    InteractionRow,
    SummaryLine,
    TasteArtifacts,
)
from kebi.db.models import InteractionType


def _make_service(
    repo_mock: AsyncMock | None = None,
) -> object:
    """Create a TasteModelService with mocked dependencies."""
    from kebi.core.taste.service import TasteModelService

    session_factory = MagicMock()
    service = TasteModelService(session_factory)
    if repo_mock is not None:
        service._repo = repo_mock
    return service


def _make_repo_mock() -> AsyncMock:
    repo = AsyncMock()
    repo.log_interaction = AsyncMock()
    repo.get_interactions_with_places = AsyncMock(return_value=[])
    repo.get_by_user_id = AsyncMock(return_value=None)
    repo.upsert_regen = AsyncMock()
    repo.count_interactions = AsyncMock(return_value=0)
    return repo


def _sample_row(type_: str = "save") -> InteractionRow:
    from kebi.core.places.models import PlaceAttributes

    return InteractionRow(
        type=type_,
        place_type="food_and_drink",
        subcategory="restaurant",
        source="tiktok",
        tags=["date-spot"],
        attributes=PlaceAttributes(cuisine="japanese", ambiance="casual"),
    )


def _sample_artifacts() -> TasteArtifacts:
    return TasteArtifacts(
        summary=[
            SummaryLine(
                text="Favors restaurant under food_and_drink.",
                signal_count=3,
                source_field="subcategory.food_and_drink",
                source_value="restaurant",
            )
        ],
    )


class TestGetTasteProfile:
    """Tests for TasteModelService.get_taste_profile defensive coercion."""

    async def test_corrupt_summary_dict_is_coerced_to_empty_list(self) -> None:
        repo = _make_repo_mock()
        taste_model = MagicMock()
        taste_model.taste_profile_summary = {}  # corrupt
        taste_model.signal_counts = {}
        taste_model.generated_from_log_count = 0
        repo.get_by_user_id.return_value = taste_model

        service = _make_service(repo)
        profile = await service.get_taste_profile("user1")

        assert profile is not None
        assert profile.taste_profile_summary == []

    async def test_valid_summary_list_is_preserved(self) -> None:
        repo = _make_repo_mock()
        taste_model = MagicMock()
        taste_model.taste_profile_summary = [
            {
                "text": "Favors restaurant under food_and_drink.",
                "signal_count": 3,
                "source_field": "subcategory.food_and_drink",
                "source_value": "restaurant",
            }
        ]
        taste_model.signal_counts = {"totals": {"saves": 5}}
        taste_model.generated_from_log_count = 5
        repo.get_by_user_id.return_value = taste_model

        service = _make_service(repo)
        profile = await service.get_taste_profile("user1")

        assert profile is not None
        assert len(profile.taste_profile_summary) == 1
        assert profile.generated_from_log_count == 5


class TestHandleSignal:
    async def test_logs_interaction_and_commits(self) -> None:
        repo = _make_repo_mock()
        service = _make_service(repo)

        with patch("kebi.core.taste.debounce.regen_debouncer") as debouncer:
            debouncer.schedule = MagicMock()
            await service.handle_signal("user1", InteractionType.SAVE, "place1")

        repo.log_interaction.assert_awaited_once_with(
            "user1", InteractionType.SAVE, "place1"
        )

    async def test_schedules_debounced_regen(self) -> None:
        repo = _make_repo_mock()
        service = _make_service(repo)

        with patch("kebi.core.taste.debounce.regen_debouncer") as debouncer:
            debouncer.schedule = MagicMock()
            await service.handle_signal("user1", InteractionType.SAVE, "place1")
            debouncer.schedule.assert_called_once()
            call_kwargs = debouncer.schedule.call_args
            assert call_kwargs.kwargs["user_id"] == "user1"


class TestRunRegen:
    async def test_min_signals_guard_skips(self) -> None:
        repo = _make_repo_mock()
        repo.get_interactions_with_places.return_value = [_sample_row()]  # only 1
        service = _make_service(repo)

        await service._run_regen("user1")
        repo.upsert_regen.assert_not_awaited()

    async def test_stale_guard_skips(self) -> None:
        rows = [_sample_row() for _ in range(5)]
        repo = _make_repo_mock()
        repo.get_interactions_with_places.return_value = rows

        taste_model = MagicMock()
        taste_model.generated_from_log_count = 5  # same as len(rows)
        repo.get_by_user_id.return_value = taste_model

        service = _make_service(repo)
        await service._run_regen("user1")
        repo.upsert_regen.assert_not_awaited()

    @patch("kebi.core.taste.service.get_llm")
    async def test_happy_path(self, mock_get_llm: MagicMock) -> None:
        rows = [_sample_row() for _ in range(5)]
        repo = _make_repo_mock()
        repo.get_interactions_with_places.return_value = rows
        repo.get_by_user_id.return_value = None

        artifacts = _sample_artifacts()
        mock_llm = AsyncMock()
        mock_llm.complete.return_value = json.dumps(artifacts.model_dump())
        mock_get_llm.return_value = mock_llm

        service = _make_service(repo)
        await service._run_regen("user1")

        repo.upsert_regen.assert_awaited_once()
        call_kwargs = repo.upsert_regen.call_args.kwargs
        assert call_kwargs["user_id"] == "user1"
        assert call_kwargs["log_count"] == 5
        assert len(call_kwargs["summary"]) > 0
        assert "chips" not in call_kwargs

    @patch("kebi.core.taste.service.get_llm")
    async def test_parse_failure_skips_regen(self, mock_get_llm: MagicMock) -> None:
        rows = [_sample_row() for _ in range(5)]
        repo = _make_repo_mock()
        repo.get_interactions_with_places.return_value = rows
        repo.get_by_user_id.return_value = None

        mock_llm = AsyncMock()
        mock_llm.complete.return_value = "not json"
        mock_get_llm.return_value = mock_llm

        service = _make_service(repo)
        await service._run_regen("user1")

        repo.upsert_regen.assert_not_awaited()
        assert mock_llm.complete.await_count == 2  # retried once
