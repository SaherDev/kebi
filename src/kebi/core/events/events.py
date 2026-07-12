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
    place_core_ids: list[str]  # places.id values (the PlaceCore id)
    place_metadata: dict[str, Any] = Field(default_factory=dict)
    request_id: str = ""


class ContentHarvestRequested(DomainEvent):
    """Event: a saved share's content should be mined into knowledge claims.

    Fired by ExtractionService after a pipeline-run save (not on the ADR-074
    cache-hit path — that content was already harvested at first extraction).
    Carries only a pointer: the durable `HarvestContent` + identified places
    live in object storage under `harvest_key`, and the handler reads them
    back to run the second pass off the critical path (ADR-121). `user_id`
    is the sharer, for tracing only — harvested claims are global
    (`user_id=NULL`), never scoped to the sharer.
    """

    event_type: str = "content_harvest_requested"
    harvest_key: str
    source_ref: str | None = None


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


class RecommendationSaved(DomainEvent):
    """Event: User saved a place kebi recommended (the consult card's "save
    it" action).

    A stronger positive than the passive `PlaceSaved` of a link-share import:
    it maps to its own taste interaction type (`saved_recommendation`) with a
    higher evidence weight, and — unlike a link-share save — does not feed the
    `source` distribution (kebi is not a discovery channel).
    """

    event_type: str = "recommendation_saved"
    recommendation_id: str
    place_core_id: str


class LibraryStateChanged(DomainEvent):
    """Event: a saved place's Library pills changed (visited/liked/approved).

    Pills are mutable snapshot state, not events, so this carries no place or
    interaction payload — it only nudges taste to re-aggregate the user's
    current pill snapshot (ADR-115). Note-only edits do not emit it, since a
    note does not affect taste. The pill-fingerprint stale-guard makes a no-op
    change (e.g. re-setting the same value) short-circuit, so emitting on any
    pill-field touch is safe.
    """

    event_type: str = "library_state_changed"


class TurnCompleted(DomainEvent):
    """Event: A user turn finished (success, clarification, or error).

    Fired by ChatService for every user turn. The memory handler appends
    `user_message` to a per-user buffer and runs LLM fact extraction on
    every Nth turn (memory.extraction.debounce_messages). The agent layer
    is unaware of fact extraction; it just emits this event.

    `surfaced_places` is the free agent-signal gate for the "what you
    wanted" recall list (ADR-110): True when the turn actually produced
    place results (a suggest/find/discover tool ran), which excludes
    chit-chat and one-word confirmations that never trigger a place search.
    """

    event_type: str = "turn_completed"
    user_message: str
    surfaced_places: bool = False
