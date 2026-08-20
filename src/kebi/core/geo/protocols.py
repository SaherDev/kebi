"""Protocol interfaces for the geo identity registry.

The registry depends on a lookup adapter through this Protocol only — a new
geocoding provider is a new adapter class, never a conditional (and never a
direct SDK import at a call site).
"""

from __future__ import annotations

from typing import Protocol

from .models import GeoArea, GeoLookupUnit, ResolvedAreaKey


class GeoLookupProtocol(Protocol):
    """The two lookups minting needs; both None on no verifiable match."""

    async def forward(
        self, *, query: str, country_code: str | None
    ) -> GeoLookupUnit | None: ...

    async def by_place_id(self, place_id: str) -> GeoLookupUnit | None: ...


class GeoRegistryProtocol(Protocol):
    """What consumers of geo identity get to ask.

    Write paths resolve with `mint=True` (a new area is worth one geocoder
    call, once, ever); read paths resolve with `mint=False` and degrade to
    coarser-but-correct when the registry hasn't met the name yet.
    """

    async def key_for_location(
        self,
        country_code: str | None,
        city: str | None,
        neighborhood: str | None,
        *,
        lat: float | None = None,
        lng: float | None = None,
        mint: bool = False,
    ) -> ResolvedAreaKey | None: ...

    async def resolve_country(
        self, name: str, *, mint: bool = True
    ) -> GeoArea | None: ...

    async def resolve_city_global(
        self, name: str, *, mint: bool = True
    ) -> GeoArea | None: ...

    async def rows_for_keys(self, geo_keys: list[str]) -> dict[str, GeoArea]: ...

    async def row_for_key(self, geo_key: str) -> GeoArea | None: ...

    async def row_for_legacy_key(self, legacy_key: str) -> GeoArea | None: ...

    async def display_row(self, row: GeoArea) -> GeoArea: ...
