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

from pydantic import BaseModel, Field

from kebi.core.places.models import PlaceCore, UserPlace


class ConsultCandidate(BaseModel):
    """One candidate returned by a consult tool.

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

    place: PlaceCore
    user_data: UserPlace | None = None
    source: Literal["saved", "suggested", "discovered"]
    rrf_score: float
    vector_rank: int | None = None
    text_rank: int | None = None
    reason: str | None = None


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

    `recommendation_id` identifies this recommendation so the client can
    attribute a later accept/reject/save signal back to it (`POST
    /v1/signal`, `POST /v1/user/places`). It is minted here, once per
    result, and returned in the tool payload; `place_core_id` on the chosen
    candidate disambiguates which place within the result the user acted on.
    """

    candidates: list[ConsultCandidate] = Field(default_factory=list)
    empty_reason: Literal["no_saves", "no_match", "no_location", "error"] | None = None
    recommendation_id: str = Field(default_factory=lambda: str(uuid4()))
