"""Boundary models for the geo identity registry.

`GeoArea` is a registry row crossing a module boundary; `GeoLookupUnit` is
one geocoder result as the lookup adapter reports it. Neither is the ORM
row — repositories translate.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

AreaSlot = Literal["country", "city", "area"]


class GeoArea(BaseModel):
    """One registered geographic unit — identity, names, geometry.

    `place_id` is the identity; `geo_key` its full id-path
    (`{cc}/{pid}` for a city-slot row, `{cc}/{city_pid}/{pid}` for an
    area-slot row). `name` is the provider's clean English name;
    `colloquial_name`/`groups_into` are the once-minted colloquial layer.
    `ambiguous` marks a unit whose one name covers several distinctly-named
    places; its `split_of` children carry the geometry that tells them apart.
    """

    model_config = ConfigDict(frozen=True)

    place_id: str
    provider: str = "google"
    country_code: str
    slot: AreaSlot
    kind: str
    name: str
    colloquial_name: str | None = None
    groups_into: str | None = None
    split_of: str | None = None
    city_place_id: str | None = None
    geo_key: str
    legacy_key: str | None = None
    lat: float | None = None
    lng: float | None = None
    # [south, west, north, east]
    viewport: list[float] | None = None
    ambiguous: bool = False

    @property
    def display_name(self) -> str:
        """The name screens and prose use — colloquial when minted, else the
        provider's."""
        return self.colloquial_name or self.name

    def contains(self, lat: float, lng: float, *, margin: float = 0.0) -> bool:
        """Point-in-viewport test, with an optional fractional margin.

        False when the row has no viewport — absence of geometry must never
        read as containment.
        """
        if self.viewport is None or len(self.viewport) != 4:
            return False
        south, west, north, east = self.viewport
        lat_pad = (north - south) * margin
        lng_pad = (east - west) * margin
        return (south - lat_pad) <= lat <= (north + lat_pad) and (
            west - lng_pad
        ) <= lng <= (east + lng_pad)


class GeoComponent(BaseModel):
    """One address component of a geocoder result."""

    model_config = ConfigDict(frozen=True)

    name: str
    short_name: str | None = None
    types: list[str] = []


class GeoLookupUnit(BaseModel):
    """One geocoder result, reduced to what minting needs.

    `name` is the unit's own component name (clean, English-pinned);
    `types` the provider's types for the feature itself; `components` the
    full chain, outermost last, for ancestor context.
    """

    model_config = ConfigDict(frozen=True)

    place_id: str
    name: str
    types: list[str] = []
    country_code: str | None = None
    lat: float
    lng: float
    # [south, west, north, east]
    viewport: list[float] | None = None
    components: list[GeoComponent] = []
    # The provider's own "this wasn't an exact match" flag — the signal
    # verification trusts instead of string heuristics: an exonym resolves
    # exactly ("Krung Thep Maha Nakhon" → Bangkok, not partial), a typo or
    # a wrong-entity fuzzy match arrives flagged.
    partial_match: bool = False


class ResolvedAreaKey(BaseModel):
    """A location triple resolved through the registry.

    `geo_key` is what gets stored or queried. `city` / `area` are the rows
    behind its segments so callers that need names or the display group
    don't re-read; either may be None when the triple resolved shallower
    than asked (coarser-but-correct, never guessed).
    """

    model_config = ConfigDict(frozen=True)

    geo_key: str
    city: GeoArea | None = None
    area: GeoArea | None = None

    @property
    def leaf(self) -> GeoArea | None:
        return self.area or self.city
