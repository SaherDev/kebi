"""Domain event models for taste model updates and recommendation feedback"""

from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class DomainEvent(BaseModel):
    """Base class for all domain events"""

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: str
    user_id: str


class PlaceSaved(DomainEvent):
    """Event: User saved a place"""

    event_type: str = "place_saved"
    place_core_ids: list[str]  # places_v2.id values (the PlaceCore id)
    place_metadata: dict[str, Any] = Field(default_factory=dict)
    request_id: str = ""


class RecommendationAccepted(DomainEvent):
    """Event: User accepted a recommendation"""

    event_type: str = "recommendation_accepted"
    recommendation_id: str
    place_core_id: str


class RecommendationRejected(DomainEvent):
    """Event: User rejected a recommendation"""

    event_type: str = "recommendation_rejected"
    recommendation_id: str
    place_core_id: str


class TurnCompleted(DomainEvent):
    """Event: A user turn finished (success, clarification, or error).

    Fired by ChatService for every user turn. The memory handler appends
    `user_message` to a per-user buffer and runs LLM fact extraction on
    every Nth turn (memory.extraction.debounce_messages). The agent layer
    is unaware of fact extraction; it just emits this event.
    """

    event_type: str = "turn_completed"
    user_message: str
