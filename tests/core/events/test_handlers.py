"""Unit tests for event handlers."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.core.events.events import (
    PlaceSaved,
    RecommendationAccepted,
    RecommendationRejected,
    RecommendationSaved,
    TurnCompleted,
)
from kebi.core.events.handlers import EventHandlers
from kebi.db.models import InteractionType


class TestOnTasteSignal:
    """Tests for the unified on_taste_signal handler (ADR-058)."""

    @pytest.fixture
    def mock_taste_service(self) -> MagicMock:
        svc = MagicMock()
        svc.handle_signal = AsyncMock()
        return svc

    @pytest.fixture
    def handlers(self, mock_taste_service: MagicMock) -> EventHandlers:
        return EventHandlers(
            taste_service=mock_taste_service,
            memory_service=MagicMock(),
            tracer=MagicMock(
                generation=MagicMock(return_value=MagicMock()),
                capture_message=MagicMock(),
                flush=MagicMock(),
            ),
        )

    async def test_place_saved_calls_handle_signal_per_place(
        self, handlers: EventHandlers, mock_taste_service: MagicMock
    ) -> None:
        event = PlaceSaved(user_id="u1", place_core_ids=["p1", "p2"], place_metadata={})
        await handlers.on_taste_signal(event)
        assert mock_taste_service.handle_signal.await_count == 2
        calls = mock_taste_service.handle_signal.call_args_list
        assert calls[0].kwargs["signal_type"] == InteractionType.SAVE
        assert calls[0].kwargs["place_core_id"] == "p1"
        assert calls[1].kwargs["place_core_id"] == "p2"

    async def test_recommendation_accepted(
        self, handlers: EventHandlers, mock_taste_service: MagicMock
    ) -> None:
        event = RecommendationAccepted(
            user_id="u1", recommendation_id="r1", place_core_id="p1"
        )
        await handlers.on_taste_signal(event)
        mock_taste_service.handle_signal.assert_awaited_once_with(
            user_id="u1", signal_type=InteractionType.ACCEPTED, place_core_id="p1"
        )

    async def test_recommendation_rejected(
        self, handlers: EventHandlers, mock_taste_service: MagicMock
    ) -> None:
        event = RecommendationRejected(
            user_id="u1", recommendation_id="r1", place_core_id="p1"
        )
        await handlers.on_taste_signal(event)
        mock_taste_service.handle_signal.assert_awaited_once_with(
            user_id="u1", signal_type=InteractionType.REJECTED, place_core_id="p1"
        )

    async def test_recommendation_saved_maps_to_dedicated_type(
        self, handlers: EventHandlers, mock_taste_service: MagicMock
    ) -> None:
        """Saving a recommendation is its own stronger signal — it maps to
        SAVED_RECOMMENDATION, not the plain SAVE bucket."""
        event = RecommendationSaved(
            user_id="u1", recommendation_id="r1", place_core_id="p1"
        )
        await handlers.on_taste_signal(event)
        mock_taste_service.handle_signal.assert_awaited_once_with(
            user_id="u1",
            signal_type=InteractionType.SAVED_RECOMMENDATION,
            place_core_id="p1",
        )

    async def test_exception_does_not_raise(
        self, handlers: EventHandlers, mock_taste_service: MagicMock
    ) -> None:
        mock_taste_service.handle_signal = AsyncMock(side_effect=RuntimeError("boom"))
        event = RecommendationAccepted(
            user_id="u1", recommendation_id="r1", place_core_id="p1"
        )
        await handlers.on_taste_signal(event)  # should not raise


class TestOnTurnCompleted:
    """Tests for EventHandlers.on_turn_completed() — thin delegation layer."""

    @pytest.fixture
    def mock_taste_service(self) -> MagicMock:
        return MagicMock()

    @pytest.fixture
    def mock_memory_service(self) -> MagicMock:
        svc = MagicMock()
        svc.extract_and_save_facts = AsyncMock()
        return svc

    @pytest.fixture
    def handlers(
        self, mock_taste_service: MagicMock, mock_memory_service: MagicMock
    ) -> EventHandlers:
        return EventHandlers(
            taste_service=mock_taste_service,
            memory_service=mock_memory_service,
            tracer=MagicMock(
                generation=MagicMock(return_value=MagicMock()),
                capture_message=MagicMock(),
                flush=MagicMock(),
            ),
        )

    async def test_delegates_to_service(
        self, handlers: EventHandlers, mock_memory_service: MagicMock
    ) -> None:
        event = TurnCompleted(user_id="user-1", user_message="I'm vegan")
        await handlers.on_turn_completed(event)
        mock_memory_service.extract_and_save_facts.assert_awaited_once_with(
            user_id="user-1",
            user_message="I'm vegan",
        )

    async def test_swallows_service_exceptions(
        self, handlers: EventHandlers, mock_memory_service: MagicMock
    ) -> None:
        """Per ADR-043, handler failures must never propagate."""
        mock_memory_service.extract_and_save_facts = AsyncMock(
            side_effect=RuntimeError("redis down")
        )
        event = TurnCompleted(user_id="user-1", user_message="anything")
        await handlers.on_turn_completed(event)  # must not raise
