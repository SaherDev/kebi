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
    place_ids: list[str]
    place_metadata: dict[str, Any] = Field(default_factory=dict)
    request_id: str = ""


class RecommendationAccepted(DomainEvent):
    """Event: User accepted a recommendation"""

    event_type: str = "recommendation_accepted"
    recommendation_id: str
    place_id: str


class RecommendationRejected(DomainEvent):
    """Event: User rejected a recommendation"""

    event_type: str = "recommendation_rejected"
    recommendation_id: str
    place_id: str


class OnboardingSignal(DomainEvent):
    """Event: User confirmed or dismissed an onboarding taste chip"""

    event_type: str = "onboarding_signal"
    place_id: str
    confirmed: bool


class ChipConfirmed(DomainEvent):
    """Event: User submitted chip_confirm selections (feature 023).

    Carries only user_id — the handler re-reads fresh chip state from the
    DB so stale payloads can't corrupt the rewrite.
    """

    event_type: str = "chip_confirmed"


class TurnCompleted(DomainEvent):
    """Event: A user turn finished (success, clarification, or error).

    Fired by ChatService for every user turn. The memory handler appends
    `user_message` to a per-user buffer and runs LLM fact extraction on
    every Nth turn (memory.extraction.debounce_messages). The agent layer
    is unaware of fact extraction; it just emits this event.
    """

    event_type: str = "turn_completed"
    user_message: str
