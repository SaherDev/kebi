"""Signal service — validates and dispatches behavioral signal events."""

from __future__ import annotations

from typing import TYPE_CHECKING

from kebi.core.events.events import (
    RecommendationAccepted,
    RecommendationRejected,
)

if TYPE_CHECKING:
    from kebi.core.events.dispatcher import EventDispatcher


# Signal types that carry a recommendation_id and dispatch a recommendation
# event. The id is no longer DB-validated — the recommendations table was
# dropped (ADR-078); the signal is trusted from the product repo.
_RECOMMENDATION_SIGNALS = frozenset(
    {
        "recommendation_accepted",
        "recommendation_rejected",
    }
)


class SignalService:
    """Validates and dispatches behavioral signal events.

    Recommendation accept/reject signals are no longer DB-validated — the
    recommendations table was dropped (ADR-078). The signal is trusted from
    the product repo and dispatched as a domain event.
    """

    def __init__(
        self,
        event_dispatcher: EventDispatcher,
    ) -> None:
        self._event_dispatcher = event_dispatcher

    async def handle_signal(
        self,
        signal_type: str,
        user_id: str,
        recommendation_id: str | None = None,
        place_core_id: str | None = None,
    ) -> None:
        """Dispatch the signal event.

        Recommendation-scoped signals carry a trusted ``recommendation_id``
        (presence enforced by the request schema); no DB lookup is performed.
        """
        if signal_type in _RECOMMENDATION_SIGNALS:
            event: RecommendationAccepted | RecommendationRejected
            if signal_type == "recommendation_accepted":
                event = RecommendationAccepted(
                    user_id=user_id,
                    recommendation_id=recommendation_id or "",
                    place_core_id=place_core_id or "",
                )
            else:
                event = RecommendationRejected(
                    user_id=user_id,
                    recommendation_id=recommendation_id or "",
                    place_core_id=place_core_id or "",
                )
            await self._event_dispatcher.dispatch(event)
