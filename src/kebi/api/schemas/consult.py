"""Request and response schemas for POST /v1/consult endpoint.

The response carries the full `PlaceObject` per result (Tier 1/2/3 fields)
so the agent tool (see `agent-tool-design.md`) can project whichever
attributes it needs without a second fetch. The ranker's normalized score
is surfaced as `confidence` on each item.
"""

from typing import Literal

from pydantic import BaseModel, Field

# Feature 027 FR-024: ReasoningStep is re-exported from the agent module
# so ConsultResponse.reasoning_steps carries the richer schema
# (source / tool_name / visibility / timestamp). The M5 consult tool
# wrapper stamps source="tool", tool_name="consult", visibility="debug"
# on every step; ConsultService builds them with those kwargs set directly.
from kebi.core.agent.reasoning import ReasoningStep as ReasoningStep
from kebi.core.places.models import PlaceObject


class Location(BaseModel):
    """User's geographic location."""

    lat: float
    lng: float


class ConsultRequest(BaseModel):
    """Request body for POST /v1/consult endpoint."""

    user_id: str
    query: str
    location: Location | None = None
    signal_tier: Literal["cold", "warming", "chip_selection", "active"] | None = Field(
        default=None,
        description=(
            "Optional tier hint (feature 023). Forwarded by the product repo "
            "from GET /v1/user/context. When null, consult defaults to 'active'."
        ),
    )


class ConsultResult(BaseModel):
    """One recommendation in the consult response.

    `place` is the fully enriched `PlaceObject` (`enriched=True`, Tier 2
    geo and Tier 3 details populated). `source` is `"saved"` when the row
    came from the user's recall set, `"discovered"` when it came from
    keyword search, or `"suggested"` when it was an agent-supplied name
    validated via the places provider. No numeric score — ranking is
    deferred to the agent (ADR-058).
    """

    place: PlaceObject
    source: str  # "saved" | "discovered" | "suggested"


class ConsultResponse(BaseModel):
    """Response body for POST /v1/consult.

    `results` is ordered by source (saved first, discovered second, suggested third) and
    capped by the agent-chosen `limit` (hard ceiling 5, enforced at the
    consult tool boundary). Reasoning steps are delivered live via the
    `emit` callback on the agent path (feature 028 M4) — the response no
    longer bundles them.
    """

    recommendation_id: str | None = Field(
        default=None,
        description="UUID from recommendations table. Null if persist failed.",
    )
    results: list[ConsultResult]
