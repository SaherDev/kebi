"""Map a resolved places PlaceCore (+ save source) to an InteractionRow.

ADR-077: taste aggregation consumes the places vocabulary. `core.tags`
is a list of PlaceTag{type, value, source}; we group it by TagType into the
typed dimensions InteractionRow exposes. `price` is single-valued (last tag
wins — matches the legacy price_hint single-value semantics). Tags whose
`type` is not a known TagType (LLM custom strings) are dropped: they have no
grounded slot in signal_counts, so keeping them would let the regen LLM cite
counts the grounding validator can't resolve.
"""

from __future__ import annotations

from kebi.core.places import PlaceCore, TagType
from kebi.core.taste.schemas import InteractionRow

# TagType -> InteractionRow list-field name. `price` is handled separately
# (single-valued) and is intentionally absent here.
_LIST_DIMENSIONS: dict[TagType, str] = {
    TagType.cuisine: "cuisine",
    TagType.dietary: "dietary",
    TagType.feature: "feature",
    TagType.atmosphere: "atmosphere",
    TagType.service: "service",
    TagType.time: "time",
    TagType.season: "season",
}


def place_to_interaction_row(
    type: str, core: PlaceCore, source: str | None
) -> InteractionRow:
    dims: dict[str, list[str]] = {name: [] for name in _LIST_DIMENSIONS.values()}
    price: str | None = None

    for tag in core.tags:
        raw_type = tag.type
        try:
            ttype = raw_type if isinstance(raw_type, TagType) else TagType(raw_type)
        except ValueError:
            continue  # unknown/custom type — no grounded slot, drop
        tval = tag.value
        value_str = tval.value if hasattr(tval, "value") else str(tval)
        if ttype is TagType.price:
            price = value_str  # last wins
            continue
        dim = _LIST_DIMENSIONS.get(ttype)
        if dim is not None:
            dims[dim].append(value_str)

    loc = core.location
    return InteractionRow(
        type=type,
        categories=[
            c.value if hasattr(c, "value") else str(c) for c in core.categories
        ],
        cuisine=dims["cuisine"],
        dietary=dims["dietary"],
        feature=dims["feature"],
        atmosphere=dims["atmosphere"],
        service=dims["service"],
        price=price,
        time=dims["time"],
        season=dims["season"],
        neighborhood=loc.neighborhood if loc else None,
        city=loc.city if loc else None,
        country=loc.country if loc else None,
        source=source,
    )
