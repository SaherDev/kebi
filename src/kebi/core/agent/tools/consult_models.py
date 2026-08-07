"""Shared output shape for the consult-family agent tools.

`find_saved`, `find_known`, and `suggest_places` all return the same
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

from pydantic import BaseModel, Field, computed_field

from kebi.core.knowledge.research_models import ResearchNote
from kebi.core.places._place_utils import display_place_name
from kebi.core.places.models import PlaceCore, UserPlace


class ConsultCandidate(BaseModel):
    """One candidate returned by a consult tool.

    `source="known"` (ADR-138) is a place the claims store named — its
    `notes` are not decoration but the reason it is in the list at all.

    `user_data` is populated for `find_saved` (the user's own row exists)
    and `None` for `find_known` / `suggest_places` (no save row
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

    place: PlaceCore
    user_data: UserPlace | None = None
    source: Literal["saved", "suggested", "discovered", "known"]
    rrf_score: float
    vector_rank: int | None = None
    text_rank: int | None = None
    reason: str | None = None
    # Which part of a multi-stop trip this candidate belongs to (ADR-148):
    # a stop's name ("Hue") or a leg ("on the way between Hue and Hoi An").
    # Only set on itinerary turns — `None` everywhere else. A leg label on a
    # save is the strongest "add this city" signal the agent gets: the user
    # never named the place's city, but it is on their route and they have a
    # reason to stop.
    segment: str | None = None
    # Insider claims kebi holds about this exact place, strongest first
    # (ADR-137). Attached on the retrieval path, not by a `research` call, so
    # a recommendation turn carries them without spending tool budget. Empty
    # when kebi knows nothing about the place yet — which is the common case
    # for a fresh discovery and must read as silence, not as a gap to fill.
    notes: list[ResearchNote] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def display_name(self) -> str:
        """The place's name as a person would write it in a sentence.

        Derived, not stored: the catalog keeps the provider's canonical
        `place_name` (and link matching still uses it), while this is what the
        agent is told to put in prose. Computed here so all four place tools
        get it without each remembering to — a provider string leaking into an
        answer ("BNI CANGGU", "Motel Mexicola | Canggu") reads as a database
        row and undoes the voice.
        """
        return display_place_name(self.place.place_name)


class ConsultResult(BaseModel):
    """Tool output payload, serialized to the ToolMessage content.

    `empty_reason` is `None` when `candidates` is non-empty and one of a
    small enum otherwise so the agent can pick the right prose
    (acknowledge no saves vs. no match vs. no location resolved).

    The tool layer does not contribute filters of its own — the agent
    builds every filter from the memory + taste context it has been
    given. So there is no "applied_hard_constraints" surfaced back:
    whatever the tool filtered by is exactly what the agent passed in,
    and the agent already knows it.

    `recommendation_id` identifies this recommendation — minted here, once
    per result, and surfaced on the chat response's `data` so a turn's
    outcome stays attributable (tracing, evals). The save path no longer
    carries it (ADR-151); it is an identifier of the turn, not of the save.
    """

    candidates: list[ConsultCandidate] = Field(default_factory=list)
    empty_reason: Literal["no_saves", "no_match", "no_location", "error"] | None = None
    recommendation_id: str = Field(default_factory=lambda: str(uuid4()))
    # Insider claims about the turn's AREA — neighborhood, city, country
    # pooled and ranked together (ADR-137). Area knowledge is what turns a
    # list of names into local advice ("monday is the big night in Canggu"),
    # and it applies to the answer as a whole rather than to any one
    # candidate, so it hangs off the result. Populated even when
    # `candidates` is empty: an empty search with real area knowledge still
    # has something honest to say.
    area_notes: list[ResearchNote] = Field(default_factory=list)
