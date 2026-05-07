"""Shared filter types for recall / consult / agent tools (feature 028 M4).

`PlaceFilters` mirrors `PlaceObject` 1:1 per ADR-056. `RecallFilters`
and `ConsultFilters` extend it with the retrieval-specific and
discovery-specific fields they each need.

`RecallFilters` lives in `core/recall/types.py` (imports and extends
`PlaceFilters` from here); `ConsultFilters` is declared here as a
sibling of `PlaceFilters`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from kebi.core.places.models import (
    PlaceAttributes,
    PlaceSource,
    PlaceType,
)


class PlaceFilters(BaseModel):
    """Shared filter base mirroring `PlaceObject` (ADR-056). All fields optional."""

    place_type: PlaceType | None = None
    subcategory: str | None = None
    tags_include: list[str] | None = None
    attributes: PlaceAttributes | None = None
    source: PlaceSource | None = None

    model_config = ConfigDict(extra="forbid")


class ConsultFilters(PlaceFilters):
    """Discovery-time filter — adds geographic search bounds only.

    Kept provider-agnostic: no Google-specific passthrough. Live-constraint
    filtering (open-now, price gating) is deferred until the agent actually
    needs it — can be added later as typed fields on this class.
    """

    place_suggestions: list[str] | None = Field(
        default=None,
        description=(
            "Specific place names drawn from your world knowledge that you "
            "believe would be a good fit given the user's taste and memory. "
            "Validated against the places provider — unconfirmed names are "
            "dropped. Limit to 5. Leave None if you have no knowledge of "
            "the location. Examples: ['Coco Tam\\'s', 'Ark Bar'] for bars "
            "in Koh Samui; ['Ichiran', 'Fuji Ramen'] for ramen in Tokyo."
        ),
    )
    radius_m: int | None = Field(
        default=None,
        description=(
            "Search radius in metres. Leave None to use the service default "
            "(1500 m). Use a larger value (e.g. 5000) when the user says "
            "'nearby' or 'around here' without specifying a tight area."
        ),
    )
    search_location_name: str | None = Field(
        default=None,
        description=(
            "Named location to search around — populate this whenever the "
            "user names a place they are NOT currently at. Examples: "
            "'find me something in Koh Samui' -> search_location_name='Koh Samui'; "
            "'restaurants in Shibuya' -> search_location_name='Shibuya Tokyo'; "
            "'what to do in Berlin' -> search_location_name='Berlin'. "
            "Leave None only when the user is asking about their current location."
        ),
    )


__all__ = ["PlaceFilters", "ConsultFilters"]
