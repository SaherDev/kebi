"""Shared output shape for the consult-family agent tools.

`find_saved`, `suggest_places`, and `discover_places` all return the same
`ConsultResult` so the agent can reason over candidates uniformly. The
only thing that varies is the `source` discriminator on each candidate —
which corpus it came from (`"saved"` / `"suggested"` / `"discovered"`).

The agent reads this in-turn from a `ToolMessage.content` JSON; the
finalize-strip node removes the message before the next checkpoint,
so nothing here persists in agent history.
"""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from kebi.core.areas.models import AreaSummary
from kebi.core.places.models import PlaceCore, UserPlace

# Why a result came back with no candidates. Named rather than inline so every
# tool declares the same closed set — a tool inventing its own reason string is
# a prose branch the agent has no instruction for.
EmptyReason = Literal["no_saves", "no_match", "no_location", "route_too_long", "error"]


class ConsultCandidate(BaseModel):
    """One candidate returned by a consult tool.

    **Kind (location-kinds Step 6).** A candidate is a `venue` or an `area`.
    `place` carries the venue, `area` carries the area, and exactly one of
    them is set. `extent` mirrors the area's believable bounding box (None for
    a venue, and None for an area whose provider geometry could not be
    trusted) — a venue renders as a pin, an area as a shaded extent.

    `id` is this item's handle within the answer. It exists so the user can
    say "take that one out" or "swap the second one" and the agent has
    something stable to map the phrase onto; the client echoes the id back
    rather than the place. Minted per candidate, per answer — it is not an
    identity that survives the conversation, because the working set it
    belongs to does not either.

    `anchor_area_key` records which area this candidate was *found at* when
    the search was anchored on areas the agent named (ADR-140). It is what
    turns a flat list into a per-area answer — "in Hoi An: …, in Hue: …" —
    and it is None on an ordinary near-me turn. On a journey it may instead
    be a stretch key (`"vn/hoi-an>vn/hue"`), because a stop on the road
    belongs to neither end of it.

    `user_data` is populated for `find_saved` (the user's own row exists)
    and `None` for `suggest_places` / `discover_places` (no save row
    yet — the user has never linked the place).

    `reason` is the namer LLM's one-line rationale for proposing this
    place. Populated only for `source="suggested"`. For
    `source="saved"` and `source="discovered"` it is left `None` on
    purpose — the per-pick reason the user sees comes from the AGENT,
    not the tool layer. The agent synthesises that reason in its prose
    answer from the candidate's structured signals (`user_data` for
    saves, `place.location` + `place.tags` + `place.categories` for
    discovered) combined with the user's taste profile, memory, and
    working-location context. Pre-computing a reason here would
    short-circuit that agent decision with a generic template.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    kind: Literal["venue", "area"] = "venue"
    place: PlaceCore | None = None
    area: AreaSummary | None = None
    extent: list[float] | None = None
    anchor_area_key: str | None = None
    # How far along a journey this sits, 0..1. Only meaningful when the agent
    # said people travel between the named areas; the answer assembler uses it
    # to order a drive as a drive rather than as a ranked list.
    route_progress: float | None = None
    user_data: UserPlace | None = None
    source: Literal["saved", "suggested", "discovered"]
    rrf_score: float
    vector_rank: int | None = None
    text_rank: int | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _one_payload_per_kind(self) -> ConsultCandidate:
        """A candidate carries the payload its kind promises, and only that.

        Enforced rather than trusted: `place` became optional in Step 6, and a
        venue candidate that silently lost its place would reach the client as
        a card with nothing in it.
        """
        if self.kind == "venue" and self.place is None:
            raise ValueError("a venue candidate requires `place`")
        if self.kind == "area" and self.area is None:
            raise ValueError("an area candidate requires `area`")
        if self.kind == "venue" and self.area is not None:
            raise ValueError("a venue candidate must not carry `area`")
        if self.kind == "area" and self.place is not None:
            raise ValueError("an area candidate must not carry `place`")
        return self

    @property
    def display_name(self) -> str:
        """The candidate's name, whichever kind it is — for summaries and logs."""
        if self.area is not None:
            return self.area.name
        return self.place.place_name if self.place is not None else ""

    @classmethod
    def for_area(
        cls,
        area: AreaSummary,
        *,
        source: Literal["saved", "suggested", "discovered"] = "suggested",
        reason: str | None = None,
        anchor_area_key: str | None = None,
        route_progress: float | None = None,
    ) -> ConsultCandidate:
        """Build an area candidate, with `extent` taken from the summary.

        The one construction path for areas, so `extent` can never disagree
        with `area.extent` — the client reads whichever it likes.

        An area can itself be *found somewhere*: Hai Van Pass is geography and
        it sits on the road between Da Nang and Hue, so it carries a placement
        like any other result.
        """
        return cls(
            kind="area",
            area=area,
            extent=area.extent,
            source=source,
            rrf_score=0.0,
            reason=reason,
            anchor_area_key=anchor_area_key,
            route_progress=route_progress,
        )


class ConsultResult(BaseModel):
    """Tool output payload, serialized to the ToolMessage content.

    `empty_reason` is `None` when `candidates` is non-empty and one of a
    small enum otherwise so the agent can pick the right prose
    (acknowledge no saves vs. no match vs. no location resolved).

    `route_too_long` (ADR-136) is not a failure — it is an answer. The turn
    is a journey whose legs are too long for venue stops to mean anything
    ("road trip from Hanoi to Saigon"): the honest stops are cities, which
    consult cannot yet return. The tool spends nothing (no LLM, no provider
    call) and the agent asks which stretch the user wants.

    The tool layer does not contribute filters of its own — the agent
    builds every filter from the memory + taste context it has been
    given. So there is no "applied_hard_constraints" surfaced back:
    whatever the tool filtered by is exactly what the agent passed in,
    and the agent already knows it.

    `recommendation_id` identifies this recommendation so the client can
    attribute a later accept/reject/save signal back to it (`POST
    /v1/signal`, `POST /v1/user/places`). It is minted here, once per
    result, and returned in the tool payload; `place_core_id` on the chosen
    candidate disambiguates which place within the result the user acted on.
    """

    candidates: list[ConsultCandidate] = Field(default_factory=list)
    empty_reason: EmptyReason | None = None
    recommendation_id: str = Field(default_factory=lambda: str(uuid4()))
