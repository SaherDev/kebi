"""Signal service — validates and dispatches behavioral signal events."""

from __future__ import annotations

from typing import TYPE_CHECKING

from kebi.core.events.events import (
    RecommendationAccepted,
    RecommendationRejected,
)

if TYPE_CHECKING:
    from kebi.core.areas.service import AreaService
    from kebi.core.events.dispatcher import EventDispatcher
    from kebi.core.taste.service import TasteModelService


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
        areas: AreaService,
        taste_service: TasteModelService,
    ) -> None:
        self._event_dispatcher = event_dispatcher
        self._areas = areas
        self._taste_service = taste_service

    async def handle_signal(
        self,
        signal_type: str,
        user_id: str,
        recommendation_id: str | None = None,
        place_core_id: str | None = None,
        entity_key: str | None = None,
    ) -> None:
        """Dispatch the signal event.

        Recommendation-scoped signals carry a trusted ``recommendation_id``
        (presence enforced by the request schema); no DB lookup is performed.
        """
        if signal_type == "area_saved":
            await self._handle_area_saved(user_id, entity_key)
            return
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

    async def _handle_area_saved(self, user_id: str, entity_key: str | None) -> None:
        """Keeping an area trains taste and writes nothing else.

        Reuses the region-interest signal ADR-135 already defined, so an area
        the user keeps and an area they shared train the same way — the
        difference is only how kebi learned they cared.

        The area is read from the entity store rather than trusted from the
        request: the signal supplies a key, and the store supplies what that
        key *is*. An unknown key is a no-op — kebi never resolved that area,
        so there is nothing truthful to record about it.
        """
        if not entity_key:
            return
        entity = await self._areas.get(entity_key)
        if entity is None:
            return
        await self._taste_service.handle_area_signal(
            user_id, entity.entity_key, entity.entity_type, entity.name
        )
