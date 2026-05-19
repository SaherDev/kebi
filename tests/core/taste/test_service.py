"""Tests for TasteModelService (ADR-077).

Covers handle_signal, _run_regen guards, the places resolve path,
orphan-skip, and the happy path.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from kebi.core.places import PlaceCategory, PlaceCore
from kebi.core.taste.schemas import (
    RawInteraction,
    SummaryLine,
    TasteArtifacts,
)
from kebi.db.models import InteractionType


def _async_ctx(session: object) -> MagicMock:
    ctx = AsyncMock()
    ctx.__aenter__.return_value = session
    ctx.__aexit__.return_value = None
    return MagicMock(return_value=ctx)


def _make_service(
    repo_mock: AsyncMock | None = None,
    cores: dict[str, PlaceCore] | None = None,
    user_places: list[object] | None = None,
) -> object:
    from kebi.core.taste.service import TasteModelService

    session_factory = _async_ctx(AsyncMock())

    search = AsyncMock()
    search.get_cores_by_ids = AsyncMock(return_value=cores or {})
    up_repo = AsyncMock()
    up_repo.get_by_user = AsyncMock(return_value=user_places or [])

    service = TasteModelService(
        session_factory,
        search_service_factory=lambda _s: search,
        user_places_repo_factory=lambda _s: up_repo,
    )
    if repo_mock is not None:
        service._repo = repo_mock
    return service


def _make_repo_mock() -> AsyncMock:
    repo = AsyncMock()
    repo.log_interaction = AsyncMock()
    repo.get_interactions = AsyncMock(return_value=[])
    repo.get_by_user_id = AsyncMock(return_value=None)
    repo.upsert_regen = AsyncMock()
    repo.count_interactions = AsyncMock(return_value=0)
    return repo


def _raw(type_: str = "save", place_core_id: str = "p1") -> RawInteraction:
    return RawInteraction(type=type_, place_core_id=place_core_id)


def _core(pid: str = "p1") -> PlaceCore:
    return PlaceCore(
        id=pid,
        place_name="Joe Pizza",
        categories=[PlaceCategory.restaurant],
    )


def _sample_artifacts() -> TasteArtifacts:
    return TasteArtifacts(
        summary=[
            SummaryLine(
                text="Favors restaurant category.",
                signal_count=5,
                source_field="categories",
                source_value="restaurant",
            )
        ],
    )


class TestGetTasteProfile:
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
                "text": "Favors restaurant category.",
                "signal_count": 3,
                "source_field": "categories",
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
    async def test_logs_interaction(self) -> None:
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
            assert debouncer.schedule.call_args.kwargs["user_id"] == "user1"


class TestRunRegen:
    async def test_min_signals_guard_skips(self) -> None:
        repo = _make_repo_mock()
        repo.get_interactions.return_value = [_raw()]  # only 1
        service = _make_service(repo)

        await service._run_regen("user1")
        repo.upsert_regen.assert_not_awaited()

    async def test_stale_guard_skips(self) -> None:
        repo = _make_repo_mock()
        repo.get_interactions.return_value = [_raw() for _ in range(5)]

        taste_model = MagicMock()
        taste_model.generated_from_log_count = 5  # same as len(raw)
        repo.get_by_user_id.return_value = taste_model

        service = _make_service(repo)
        await service._run_regen("user1")
        repo.upsert_regen.assert_not_awaited()

    @patch("kebi.core.taste.service.get_llm")
    async def test_happy_path(self, mock_get_llm: MagicMock) -> None:
        repo = _make_repo_mock()
        repo.get_interactions.return_value = [_raw() for _ in range(5)]
        repo.get_by_user_id.return_value = None

        mock_llm = AsyncMock()
        mock_llm.complete.return_value = json.dumps(
            _sample_artifacts().model_dump()
        )
        mock_get_llm.return_value = mock_llm

        service = _make_service(repo, cores={"p1": _core("p1")})
        await service._run_regen("user1")

        repo.upsert_regen.assert_awaited_once()
        kwargs = repo.upsert_regen.call_args.kwargs
        assert kwargs["user_id"] == "user1"
        assert kwargs["log_count"] == 5
        assert len(kwargs["summary"]) > 0
        # places vocabulary persisted
        assert kwargs["signal_counts"]["categories"] == {"restaurant": 5}
        assert "place_type" not in kwargs["signal_counts"]

    @patch("kebi.core.taste.service.get_llm")
    async def test_orphan_place_skipped(self, mock_get_llm: MagicMock) -> None:
        """Interactions whose place_id doesn't resolve are dropped."""
        repo = _make_repo_mock()
        repo.get_interactions.return_value = [
            _raw(place_core_id="gone") for _ in range(5)
        ]
        repo.get_by_user_id.return_value = None

        mock_llm = AsyncMock()
        mock_llm.complete.return_value = json.dumps({"summary": []})
        mock_get_llm.return_value = mock_llm

        # cores is empty → every interaction is an orphan
        service = _make_service(repo, cores={})
        await service._run_regen("user1")

        # Still persists (empty signal_counts), but nothing aggregated.
        repo.upsert_regen.assert_awaited_once()
        sc = repo.upsert_regen.call_args.kwargs["signal_counts"]
        assert sc["categories"] == {}
        assert sc["totals"]["saves"] == 0

    @patch("kebi.core.taste.service.get_llm")
    async def test_parse_failure_skips_regen(self, mock_get_llm: MagicMock) -> None:
        repo = _make_repo_mock()
        repo.get_interactions.return_value = [_raw() for _ in range(5)]
        repo.get_by_user_id.return_value = None

        mock_llm = AsyncMock()
        mock_llm.complete.return_value = "not json"
        mock_get_llm.return_value = mock_llm

        service = _make_service(repo, cores={"p1": _core("p1")})
        await service._run_regen("user1")

        repo.upsert_regen.assert_not_awaited()
        assert mock_llm.complete.await_count == 2  # retried once
