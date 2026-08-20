"""GeoRegistry behavior over an in-memory repo and a scripted lookup.

These are the cases the deleted fold tables used to pin, now expressed as
what the registry *does*: two names for one unit key once (the provider id
merges them), colloquial grouping folds identity at the one resolution
point, and an ambiguous unit resolves per point through its minted splits.
"""

from __future__ import annotations

from kebi.core.geo.models import GeoArea, GeoComponent, GeoLookupUnit
from kebi.core.geo.registry import GeoRegistry, _names_agree, _verified
from kebi.core.knowledge.schemas import _slugify


class InMemoryGeoRepo:
    """A faithful, tiny GeoAreaRepository."""

    def __init__(self) -> None:
        self.rows: dict[str, GeoArea] = {}
        self.aliases: dict[tuple[str, str, str], str] = {}

    async def get(self, place_id: str) -> GeoArea | None:
        return self.rows.get(place_id)

    async def get_many(self, place_ids: list[str]) -> dict[str, GeoArea]:
        return {p: self.rows[p] for p in place_ids if p in self.rows}

    async def get_by_key(self, geo_key: str) -> GeoArea | None:
        return next((r for r in self.rows.values() if r.geo_key == geo_key), None)

    async def get_by_keys(self, geo_keys: list[str]) -> dict[str, GeoArea]:
        return {r.geo_key: r for r in self.rows.values() if r.geo_key in geo_keys}

    async def get_by_legacy_key(self, legacy_key: str) -> GeoArea | None:
        return next((r for r in self.rows.values() if r.legacy_key == legacy_key), None)

    async def get_splits(self, place_id: str) -> list[GeoArea]:
        return [r for r in self.rows.values() if r.split_of == place_id]

    async def lookup_alias(
        self, country_code: str, city_place_id: str, slug: str
    ) -> GeoArea | None:
        pid = self.aliases.get((country_code, city_place_id, slug))
        return self.rows.get(pid) if pid else None

    async def lookup_country_alias(self, slug: str) -> GeoArea | None:
        for (_, scope, alias_slug), pid in self.aliases.items():
            if scope == "country" and alias_slug == slug:
                return self.rows.get(pid)
        return None

    async def upsert(self, area: GeoArea) -> GeoArea:
        existing = self.rows.get(area.place_id)
        if existing is not None and area.legacy_key is None:
            area = area.model_copy(update={"legacy_key": existing.legacy_key})
        self.rows[area.place_id] = area
        return area

    async def add_alias(
        self, country_code: str, city_place_id: str, slug: str, place_id: str
    ) -> None:
        self.aliases.setdefault((country_code, city_place_id, slug), place_id)

    async def set_ambiguous(self, place_id: str) -> None:
        row = self.rows[place_id]
        self.rows[place_id] = row.model_copy(update={"ambiguous": True})

    async def set_legacy_key(self, place_id: str, legacy_key: str) -> None:
        row = self.rows[place_id]
        self.rows[place_id] = row.model_copy(update={"legacy_key": legacy_key})


def unit(
    pid: str,
    name: str,
    *,
    cc: str = "id",
    types: list[str] | None = None,
    lat: float = 0.0,
    lng: float = 0.0,
    viewport: list[float] | None = None,
    partial_match: bool = False,
) -> GeoLookupUnit:
    return GeoLookupUnit(
        place_id=pid,
        name=name,
        types=types or ["administrative_area_level_4", "political"],
        country_code=cc,
        lat=lat,
        lng=lng,
        viewport=viewport,
        components=[GeoComponent(name=name, types=types or [])],
        partial_match=partial_match,
    )


class ScriptedLookup:
    """forward() answers from a slug→unit script; every call is recorded."""

    def __init__(self, script: dict[str, GeoLookupUnit]) -> None:
        self.script = script
        self.calls: list[str] = []

    async def forward(
        self, *, query: str, country_code: str | None
    ) -> GeoLookupUnit | None:
        self.calls.append(query)
        name = query.split(",")[0].strip()
        return self.script.get(_slugify(name))

    async def by_place_id(self, place_id: str) -> GeoLookupUnit | None:
        return next((u for u in self.script.values() if u.place_id == place_id), None)


def registry(
    script: dict[str, GeoLookupUnit], repo: InMemoryGeoRepo | None = None
) -> tuple[GeoRegistry, InMemoryGeoRepo, ScriptedLookup]:
    repo = repo or InMemoryGeoRepo()
    lookup = ScriptedLookup(script)
    # No instructor client: the colloquial pass is exercised separately.
    reg = GeoRegistry(repo=repo, lookup=lookup, instructor_client=None)
    return reg, repo, lookup


_BALI = unit("CityBali01", "Bali", types=["administrative_area_level_1"])
_CANGGU = unit(
    "AreaCanggu01",
    "Canggu",
    lat=-8.64,
    lng=115.14,
    viewport=[-8.66, 115.12, -8.61, 115.16],
)


class TestNameVarianceMerges:
    async def test_two_spellings_of_one_city_key_once(self) -> None:
        """The Bangkok/Krung Thep class: different words, one provider id —
        the second ask joins by id with no second mint."""
        bkk = unit("CityBangkok1", "Bangkok", cc="th", types=["locality"])
        reg, repo, lookup = registry({"bangkok": bkk, "krung-thep-maha-nakhon": bkk})

        first = await reg.key_for_location("th", "Bangkok", None, mint=True)
        second = await reg.key_for_location(
            "th", "Krung Thep Maha Nakhon", None, mint=True
        )

        assert first is not None and second is not None
        assert first.geo_key == second.geo_key == "th/CityBangkok1"
        assert len(repo.rows) == 1

    async def test_second_ask_by_known_alias_makes_no_lookup_call(self) -> None:
        reg, repo, lookup = registry({"bali": _BALI})
        await reg.key_for_location("id", "Bali", None, mint=True)
        calls_after_mint = len(lookup.calls)

        await reg.key_for_location("id", "Bali", None, mint=True)

        assert len(lookup.calls) == calls_after_mint


class TestVerifiedOrRefuse:
    async def test_a_name_the_provider_disagrees_with_is_refused(self) -> None:
        """Asked "Muine", answered "Da Nang" flagged partial — the ADR-126
        class. No row, no alias, and the key degrades to the country."""
        wrong = unit(
            "CityDaNang01",
            "Da Nang",
            cc="vn",
            types=["locality"],
            partial_match=True,
        )
        reg, repo, _ = registry({"muine": wrong})

        resolved = await reg.key_for_location("vn", "Muine", None, mint=True)

        assert resolved is not None and resolved.geo_key == "vn"
        assert repo.rows == {}

    async def test_verification_is_the_providers_exactness_signal(self) -> None:
        # An exonym resolves exactly — verified despite the name difference.
        assert _verified("Krung Thep Maha Nakhon", unit("X1", "Bangkok", cc="th"))
        # A fuzzy/partial resolution verifies only when the names agree —
        # "Kansas" must never verify as "Kansas City".
        assert not _verified(
            "Kansas", unit("X2", "Kansas City", cc="us", partial_match=True)
        )
        assert _verified(
            "Bang Rak", unit("X3", "Bang Rak", cc="th", partial_match=True)
        )
        # Names-agree stays strict slug equality across scripts.
        assert _names_agree("Hội An", "Hoi An")
        assert not _names_agree("Kansas", "Kansas City")

    async def test_read_paths_never_mint(self) -> None:
        reg, repo, lookup = registry({"bali": _BALI})

        resolved = await reg.key_for_location("id", "Bali", None, mint=False)

        assert resolved is not None and resolved.geo_key == "id"
        assert lookup.calls == []
        assert repo.rows == {}

    async def test_no_country_code_is_the_elsewhere_bucket(self) -> None:
        reg, _, _ = registry({})
        assert await reg.key_for_location(None, "Bali", None) is None
        assert await reg.key_for_location("indonesia", "Bali", None) is None


class TestAreaResolution:
    async def test_area_mints_under_its_city(self) -> None:
        reg, repo, _ = registry({"bali": _BALI, "canggu": _CANGGU})

        resolved = await reg.key_for_location("id", "Bali", "Canggu", mint=True)

        assert resolved is not None
        assert resolved.geo_key == "id/CityBali01/AreaCanggu01"
        assert resolved.area is not None and resolved.area.name == "Canggu"

    async def test_a_neighborhood_that_is_the_city_keys_at_city_depth(self) -> None:
        reg, repo, _ = registry({"bali": _BALI})

        resolved = await reg.key_for_location("id", "Bali", "Bali", mint=True)

        assert resolved is not None and resolved.geo_key == "id/CityBali01"
        assert resolved.area is None

    async def test_grouped_unit_folds_to_its_display_group_key(self) -> None:
        """The Tibubeneng→Canggu class, as data: a row whose `groups_into`
        points at Canggu keys as Canggu everywhere."""
        reg, repo, _ = registry({"bali": _BALI, "canggu": _CANGGU})
        await reg.key_for_location("id", "Bali", "Canggu", mint=True)
        tibubeneng = GeoArea(
            place_id="AreaTibubeneng1",
            country_code="id",
            slot="area",
            kind="administrative_area_level_4",
            name="Tibubeneng",
            city_place_id="CityBali01",
            geo_key="id/CityBali01/AreaTibubeneng1",
            groups_into="AreaCanggu01",
        )
        await repo.upsert(tibubeneng)
        await repo.add_alias("id", "CityBali01", "tibubeneng", "AreaTibubeneng1")

        resolved = await reg.key_for_location("id", "Bali", "Tibubeneng", mint=True)

        assert resolved is not None
        assert resolved.geo_key == "id/CityBali01/AreaCanggu01"

    async def test_ambiguous_unit_resolves_per_point_through_splits(self) -> None:
        """The Gili class: one desa name, three islands. The point decides,
        through minted geometry — no name table anywhere."""
        reg, repo, _ = registry({"bali": _BALI})
        desa = GeoArea(
            place_id="AreaGiliIndah1",
            country_code="id",
            slot="area",
            kind="administrative_area_level_4",
            name="Gili Indah",
            city_place_id="CityBali01",
            geo_key="id/CityBali01/AreaGiliIndah1",
            ambiguous=True,
        )
        air = GeoArea(
            place_id="AreaGiliAir1",
            country_code="id",
            slot="area",
            kind="natural_feature",
            name="Gili Air",
            city_place_id="CityBali01",
            split_of="AreaGiliIndah1",
            geo_key="id/CityBali01/AreaGiliAir1",
            viewport=[-8.37, 116.07, -8.35, 116.09],
        )
        for row in (desa, air):
            await repo.upsert(row)
        await repo.add_alias("id", "CityBali01", "gili-indah", "AreaGiliIndah1")

        on_air = await reg.key_for_location(
            "id", "Bali", "Gili Indah", lat=-8.3577, lng=116.0819, mint=True
        )
        no_point = await reg.key_for_location("id", "Bali", "Gili Indah", mint=True)

        assert on_air is not None and on_air.geo_key.endswith("AreaGiliAir1")
        # Without a point the desa itself is the honest answer.
        assert no_point is not None and no_point.geo_key.endswith("AreaGiliIndah1")


class TestCountries:
    async def test_country_name_resolves_to_its_code_key(self) -> None:
        indo = unit("CountryID1", "Indonesia", types=["country", "political"])
        reg, repo, _ = registry({"indonesia": indo})

        row = await reg.resolve_country("Indonesia")

        assert row is not None and row.geo_key == "id"
        # Cached: the next ask joins by alias.
        again = await reg.resolve_country("Indonesia")
        assert again is not None and again.place_id == row.place_id

    async def test_a_city_never_verifies_as_a_country(self) -> None:
        bali = unit("CityBali01", "Bali", types=["administrative_area_level_1"])
        reg, _, _ = registry({"bali": bali})

        assert await reg.resolve_country("Bali") is None


class TestLegacyKeys:
    async def test_a_recorded_legacy_key_finds_its_row(self) -> None:
        reg, repo, _ = registry({"bali": _BALI, "canggu": _CANGGU})
        resolved = await reg.key_for_location("id", "Bali", "Canggu", mint=True)
        assert resolved is not None and resolved.area is not None
        await repo.set_legacy_key(resolved.area.place_id, "id/bali/canggu")

        row = await reg.row_for_legacy_key("id/bali/canggu")

        assert row is not None and row.geo_key == "id/CityBali01/AreaCanggu01"
