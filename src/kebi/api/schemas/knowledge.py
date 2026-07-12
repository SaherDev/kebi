"""Request/response schemas for the knowledge curation endpoint (ADR-121)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from kebi.core.knowledge.curator import LocationHint
from kebi.core.knowledge.schemas import StructuredClaim


class CurateRequest(BaseModel):
    """An expert's prose to structure into curated knowledge claims.

    `location_hint` is an optional anchor used as the fallback geography when
    a claim's area can't be geocoded from the prose alone.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    location_hint: LocationHint | None = None


class CuratedClaimView(BaseModel):
    """One stored claim, as echoed back to the curator (ADR-105 — an explicit
    projection, never a persistence model serialized directly)."""

    scope: str
    entity_name: str
    claim: str
    tags: list[str]

    @classmethod
    def from_claim(cls, claim: StructuredClaim) -> CuratedClaimView:
        return cls(
            scope=claim.scope,
            entity_name=claim.entity_name,
            claim=claim.claim,
            tags=list(claim.tags),
        )


class CurateResponse(BaseModel):
    """What the curate endpoint returns: how many new claims were stored and a
    view of them. `claims_written` may be less than the prose implied — dedup
    collapses re-submissions and unkeyable/accessibility claims are dropped."""

    claims_written: int
    claims: list[CuratedClaimView]

    @classmethod
    def from_written(cls, written: list[StructuredClaim]) -> CurateResponse:
        return cls(
            claims_written=len(written),
            claims=[CuratedClaimView.from_claim(c) for c in written],
        )
