"""Request/response schemas for the knowledge curation endpoints (ADR-121)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kebi.core.areas.keys import encode_area_id
from kebi.core.knowledge.entity_search_service import AreaHit, EntitySearchResults
from kebi.core.knowledge.schemas import KnowledgeClaim, WrittenClaim
from kebi.core.places.models import PlaceCore


class CurateAnchor(BaseModel):
    """The entity a curation request is pinned to — exactly one reference.

    `place_id` is a catalog id (`places.id`, the same id venue links carry);
    `area_id` is the encoded area token (the same token `kebi://area/{id}`
    links carry), decoded server-side only via `core/areas/keys.py`.
    """

    model_config = ConfigDict(extra="forbid")

    place_id: str | None = None
    area_id: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> CurateAnchor:
        if (self.place_id is None) == (self.area_id is None):
            raise ValueError("anchor must set exactly one of place_id, area_id")
        return self


class CurateRequest(BaseModel):
    """An expert's prose to structure into curated knowledge claims.

    `anchor` pins the prose to one entity: it enables venue-scoped claims
    (a `place_id` anchor) and supplies the fallback geography when a claim's
    area can't be geocoded from the prose alone. Unanchored prose is
    geo-scoped only.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    anchor: CurateAnchor | None = None


class CuratedClaimView(BaseModel):
    """One stored claim, as echoed back to the curator (ADR-105 — an explicit
    projection, never a persistence model serialized directly). `id` is the
    stored row's id — the reference the delete path takes."""

    id: str
    scope: str
    entity_name: str
    claim: str
    tags: list[str]

    @classmethod
    def from_written(cls, written: WrittenClaim) -> CuratedClaimView:
        return cls(
            id=written.id,
            scope=written.claim.scope,
            entity_name=written.claim.entity_name,
            claim=written.claim.claim,
            tags=list(written.claim.tags),
        )


class CurateResponse(BaseModel):
    """What the curate endpoint returns: how many new claims were stored and a
    view of them. `claims_written` may be less than the prose implied — dedup
    collapses re-submissions and unkeyable/accessibility claims are dropped."""

    claims_written: int
    claims: list[CuratedClaimView]

    @classmethod
    def from_written(cls, written: list[WrittenClaim]) -> CurateResponse:
        return cls(
            claims_written=len(written),
            claims=[CuratedClaimView.from_written(w) for w in written],
        )


class MyClaimsQuery(BaseModel):
    """Query params for the my-claims browse endpoint."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(20, ge=1, le=100, description="Max claims per page.")
    cursor: str | None = Field(
        None,
        description=(
            "Opaque pagination cursor from a prior response's `next_cursor`. "
            "Omit for the first page."
        ),
    )


class ClaimAnchorView(BaseModel):
    """The entity a stored claim hangs on, as something the client can render
    and open. A place anchor carries the catalog id venue links use; every
    geo scope IS an area, so its anchor carries the same encoded token
    `kebi://area/{id}` links use — both are tappable as-is."""

    type: Literal["place", "area"]
    place_id: str | None = None
    area_id: str | None = None
    name: str


class MyClaimView(BaseModel):
    """One of the caller's own curated claims (ADR-105 projection —
    provenance, confidence, and review internals never leave the service)."""

    id: str
    scope: str
    claim: str
    tags: list[str]
    created_at: datetime
    anchor: ClaimAnchorView

    @classmethod
    def from_claim(cls, record: KnowledgeClaim) -> MyClaimView:
        if record.entity_type == "place":
            anchor = ClaimAnchorView(
                type="place",
                place_id=record.entity_key.partition(":")[2],
                name=record.entity_name,
            )
        else:
            anchor = ClaimAnchorView(
                type="area",
                area_id=encode_area_id(record.entity_key),
                name=record.entity_name,
            )
        return cls(
            id=record.id,
            scope=record.entity_type,
            claim=record.claim,
            tags=list(record.tags),
            created_at=record.created_at,
            anchor=anchor,
        )


class MyClaimsResponse(BaseModel):
    """One newest-first page of the caller's curated claims."""

    claims: list[MyClaimView]
    next_cursor: str | None

    @classmethod
    def from_page(
        cls, records: list[KnowledgeClaim], next_cursor: str | None
    ) -> MyClaimsResponse:
        return cls(
            claims=[MyClaimView.from_claim(r) for r in records],
            next_cursor=next_cursor,
        )


class EntitySearchQuery(BaseModel):
    """Query params for the anchor-chip typeahead."""

    model_config = ConfigDict(extra="forbid")

    q: str = Field(min_length=2, max_length=120, description="Name fragment.")
    limit: int = Field(8, ge=1, le=20, description="Max results.")


class EntityResultView(BaseModel):
    """One typeahead result. The id field IS the anchor payload: `place_id`
    goes into a curate anchor (and `kebi://venue/{id}` uses the same id);
    `area_id` is the encoded token area links and `GET /v1/areas/{id}` take.
    `context` is one display line for disambiguation; `icon` and `level`
    render the row (icon nullable — the client keeps its category fallback,
    ADR-146)."""

    type: Literal["place", "area"]
    place_id: str | None = None
    area_id: str | None = None
    name: str
    level: str | None = None
    icon: str | None = None
    context: str | None = None

    @classmethod
    def from_area(cls, hit: AreaHit) -> EntityResultView:
        # Context (ancestor names) is composed by the search service from
        # registry/profile data — an id-key segment has nothing readable
        # to derive it from here.
        return cls(
            type="area",
            area_id=encode_area_id(hit.geo_key),
            name=hit.name,
            level=hit.level,
            context=hit.context,
        )

    @classmethod
    def from_place(cls, place: PlaceCore) -> EntityResultView:
        loc = place.location
        parts = (
            [p for p in (loc.neighborhood, loc.city, loc.country) if p] if loc else []
        )
        return cls(
            type="place",
            place_id=place.id,
            name=place.place_name,
            icon=place.icon,
            context=", ".join(parts[:2]) or None,
        )


class EntitySearchResponse(BaseModel):
    """One ranked list: areas first, then places in hybrid-search order."""

    results: list[EntityResultView]

    @classmethod
    def from_results(cls, results: EntitySearchResults) -> EntitySearchResponse:
        return cls(
            results=[
                *(EntityResultView.from_area(a) for a in results.areas),
                *(
                    EntityResultView.from_place(p)
                    for p in results.places
                    if p.id is not None
                ),
            ]
        )
