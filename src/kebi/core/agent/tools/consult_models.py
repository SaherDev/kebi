"""Shared output shape for the consult-family agent tools.

`find_saved`, `search_suggested`, and `discover_others` (the last two
land in follow-ups) all return the same `ConsultResult` so the agent
can reason over candidates uniformly. The only thing that varies is
the `source` discriminator on each candidate — which corpus it came
from.

The agent reads this in-turn from a `ToolMessage.content` JSON; the
finalize-strip node removes the message before the next checkpoint,
so nothing here persists in agent history.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from kebi.core.places.models import PlaceCore, UserPlace


class ConsultCandidate(BaseModel):
    """One candidate returned by a consult tool.

    `user_data` is populated for `find_saved` (the user's own row exists)
    and `None` for `suggest_places` / `discover_others` (no save row
    yet — the user has never linked the place).

    `reason` is the namer LLM's one-line rationale for proposing this
    place. Populated for `source="suggested"` and left `None` for
    `source="saved"` / `source="discovered"`, which surface places the
    LLM did not name.
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
    """

    candidates: list[ConsultCandidate] = Field(default_factory=list)
    empty_reason: Literal["no_saves", "no_match", "no_location", "error"] | None = None
