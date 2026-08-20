"""In-memory GeoRegistry fake for tests.

Implements `GeoRegistryProtocol` over seeded rows: resolution is an alias
lookup exactly like the real registry, minus the geocoder — `mint=True`
resolves only what was seeded, which is what unit tests want (a test that
needs a mintable unit seeds it first). Slug aliases are auto-registered
from each seeded row's names, and per-test extras can be added.

Seed with `make_country` / `make_city` / `make_area`; pids must carry an
uppercase character so keys read as id-grammar, never legacy.
"""

from __future__ import annotations

from kebi.core.geo.models import GeoArea, ResolvedAreaKey
from kebi.core.knowledge.schemas import _slugify


def make_country(
    cc: str, name: str, *, pid: str | None = None, **kwargs: object
) -> GeoArea:
    return GeoArea(
        place_id=pid or f"Country{name.title().replace(' ', '')}",
        country_code=cc,
        slot="country",
        kind="country",
        name=name,
        geo_key=cc,
        **kwargs,  # type: ignore[arg-type]
    )


def make_city(
    cc: str, name: str, *, pid: str | None = None, **kwargs: object
) -> GeoArea:
    pid = pid or f"City{name.title().replace(' ', '')}"
    return GeoArea(
        place_id=pid,
        country_code=cc,
        slot="city",
        kind="locality",
        name=name,
        geo_key=f"{cc}/{pid}",
        **kwargs,  # type: ignore[arg-type]
    )


def make_area(
    city: GeoArea, name: str, *, pid: str | None = None, **kwargs: object
) -> GeoArea:
    pid = pid or f"Area{name.title().replace(' ', '')}"
    return GeoArea(
        place_id=pid,
        country_code=city.country_code,
        slot="area",
        kind="administrative_area_level_4",
        name=name,
        city_place_id=city.place_id,
        geo_key=f"{city.geo_key}/{pid}",
        **kwargs,  # type: ignore[arg-type]
    )


class FakeGeoRegistry:
    """Seeded, deterministic registry. No network, no minting side effects."""

    def __init__(self, *rows: GeoArea) -> None:
        self.by_pid: dict[str, GeoArea] = {}
        self.by_key: dict[str, GeoArea] = {}
        # (cc, scope, slug) -> pid; scope '' = city slot, else city pid.
        self.aliases: dict[tuple[str, str, str], str] = {}
        self.country_aliases: dict[str, str] = {}
        self.legacy: dict[str, str] = {}
        for row in rows:
            self.add(row)

    def add(self, row: GeoArea, *alias_names: str) -> GeoArea:
        self.by_pid[row.place_id] = row
        self.by_key[row.geo_key] = row
        names = [row.name, row.colloquial_name, *alias_names]
        for name in names:
            if not name:
                continue
            slug = _slugify(name)
            if row.slot == "country":
                self.country_aliases[slug] = row.place_id
            elif row.slot == "city":
                self.aliases[(row.country_code, "", slug)] = row.place_id
            else:
                self.aliases[(row.country_code, row.city_place_id or "", slug)] = (
                    row.place_id
                )
        if row.legacy_key:
            self.legacy[row.legacy_key] = row.place_id
        return row

    # ---- GeoRegistryProtocol ------------------------------------------

    async def key_for_location(
        self,
        country_code: str | None,
        city: str | None,
        neighborhood: str | None,
        *,
        lat: float | None = None,
        lng: float | None = None,
        mint: bool = False,
    ) -> ResolvedAreaKey | None:
        if not country_code:
            return None
        cc = country_code.strip().lower()
        if len(cc) != 2 or not cc.isalpha():
            return None
        if not city:
            return ResolvedAreaKey(geo_key=cc)
        city_pid = self.aliases.get((cc, "", _slugify(city)))
        city_row = self.by_pid.get(city_pid) if city_pid else None
        if city_row is None:
            return ResolvedAreaKey(geo_key=cc)
        if not neighborhood:
            return ResolvedAreaKey(geo_key=city_row.geo_key, city=city_row)
        area_pid = self.aliases.get((cc, city_row.place_id, _slugify(neighborhood)))
        area_row = self.by_pid.get(area_pid) if area_pid else None
        if area_row is None:
            return ResolvedAreaKey(geo_key=city_row.geo_key, city=city_row)
        area_row = self._disambiguate(area_row, lat, lng)
        if area_row.groups_into and not area_row.split_of:
            area_row = self.by_pid.get(area_row.groups_into, area_row)
        return ResolvedAreaKey(geo_key=area_row.geo_key, city=city_row, area=area_row)

    def _disambiguate(
        self, row: GeoArea, lat: float | None, lng: float | None
    ) -> GeoArea:
        if not row.ambiguous or lat is None or lng is None:
            return row
        splits = [s for s in self.by_pid.values() if s.split_of == row.place_id]
        containing = [s for s in splits if s.contains(lat, lng)]
        return containing[0] if containing else row

    async def resolve_country(self, name: str, *, mint: bool = True) -> GeoArea | None:
        pid = self.country_aliases.get(_slugify(name))
        return self.by_pid.get(pid) if pid else None

    async def resolve_city_global(
        self, name: str, *, mint: bool = True
    ) -> GeoArea | None:
        slug = _slugify(name)
        for (_, scope, alias_slug), pid in self.aliases.items():
            if scope == "" and alias_slug == slug:
                return self.by_pid.get(pid)
        return None

    async def rows_for_keys(self, geo_keys: list[str]) -> dict[str, GeoArea]:
        return {k: self.by_key[k] for k in geo_keys if k in self.by_key}

    async def row_for_key(self, geo_key: str) -> GeoArea | None:
        return self.by_key.get(geo_key)

    async def row_for_legacy_key(self, legacy_key: str) -> GeoArea | None:
        pid = self.legacy.get(legacy_key)
        return self.by_pid.get(pid) if pid else None

    async def display_row(self, row: GeoArea) -> GeoArea:
        if row.groups_into is None:
            return row
        return self.by_pid.get(row.groups_into, row)
