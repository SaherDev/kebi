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


class WebFindingsHarvestRequested(DomainEvent):
    """Event: a turn's web-search findings should be mined into claims.

    Fired by ChatService when a turn actually ran `web_search` and got
    something back. Unlike the content harvest this carries the findings
    inline rather than a bucket pointer: they are a few hundred bytes that
    already exist in memory, and a durable snapshot would be storing search
    results permanently to mine them once.

    Runs after the response is sent, so the user never waits on it (ADR-145).
    `user_id` is for tracing only — claims mined here are global
    (`user_id=NULL`), because a fact about an area is not personal.
    """

    event_type: str = "web_findings_harvest_requested"
    # A `WebSearchResult`, serialised. Kept as a dict so the events module
    # stays free of core-domain imports, as every other event here does.
    result: dict[str, Any]


class RecommendationSaved(DomainEvent):
    """Event: User saved a place kebi surfaced (the place screen's "save"
    action, ADR-151 — no recommendation attribution rides along anymore).

    A stronger positive than the passive `PlaceSaved` of a link-share import:
    it maps to its own taste interaction type (`saved_recommendation`) with a
    higher evidence weight, and — unlike a link-share save — does not feed the
    `source` distribution (kebi is not a discovery channel).
    """

    event_type: str = "recommendation_saved"
    place_core_id: str


class PlaceProfileRequested(DomainEvent):
    """Event: a thin catalog row was opened and should be profiled (ADR-152).

    Fired by the place-detail route when the row carries no experiential
    tags — i.e. it entered through the provider write-through and no LLM has
    ever looked at it. The handler runs one identity-only profiling call and
    persists the tags (and icon, if missing) onto the catalog row: global,
    once per place, so the cost is bounded by places users actually open.
    `user_id` is the opener, for tracing only — the enrichment is never
    user-scoped.
    """

    event_type: str = "place_profile_requested"
    place_id: str


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
