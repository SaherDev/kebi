"""GeoRegistry — resolve location names to registered identity, minting once.

The registry answers one question everywhere geo identity is needed: *which
unit is this name, here?* Resolution is an alias lookup (no network); a miss
on a write path mints the unit — one geocoder call verifies the name against
the provider's own record of it (round-trip doctrine, ADR-126/160), the row
stores the id, clean name, and geometry, and an LLM pass records the
colloquial layer, code-verified before anything lands. Read paths never
mint: an unknown name degrades to a coarser-but-correct key, and heals the
first time a write meets it.

The one deliberate LLM-in-geo departure: the colloquial layer (what people
call a unit, the bigger area it belongs to, the distinct places one admin
name covers) is model knowledge the geocoder simply does not carry — Google
will never say a Berawa villa is "in Canggu", and reverse geocoding a Gili
Air cafe names only the desa. Identity never comes from the model: every
name it offers must forward-geocode to a verified unit or it is discarded.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, Field

from kebi.core.agent._trace_context import traced_call
from kebi.core.config import get_prompt
from kebi.core.knowledge.schemas import _slugify

from .google_lookup import GeoLookupError
from .models import GeoArea, GeoLookupUnit, ResolvedAreaKey

if TYPE_CHECKING:
    from kebi.db.repositories.geo_area_repository import GeoAreaRepository
    from kebi.providers.llm import InstructorClient

    from .protocols import GeoLookupProtocol

logger = logging.getLogger(__name__)

# A `covers` split set is stored all-or-nothing: with only one verified
# member, a point on any *other* member would resolve to the one that
# happened to verify — worse than staying coarse.
_MIN_SPLITS = 2
_MAX_SPLITS = 6
_MAX_NAME_LEN = 60
# A split member must sit inside (or near) its parent's viewport; the margin
# absorbs provider viewports that crop an island's beach.
_SPLIT_CONTAINMENT_MARGIN = 0.5
# Alias scope markers in geo_area_aliases: '' scopes a city-slot alias to
# its country; the literal 'country' marks a country-name alias (queried
# without a country code — the code is what's being resolved).
_CITY_SCOPE = ""
_COUNTRY_SCOPE = "country"


class _MintResponse(BaseModel):
    """Structured output of the colloquial-layer call.

    Every name here is a *candidate*: it becomes data only after it
    forward-geocodes to a verified unit. `covers` is for one admin name
    spanning several distinctly-named places (a multi-island desa); empty
    for a normal unit.
    """

    colloquial_name: str | None = None
    part_of: str | None = None
    covers: list[str] = Field(default_factory=list)


def _names_agree(asked: str, returned: str) -> bool:
    """Slug equality across scripts and diacritics — nothing looser.

    Deliberately no containment heuristic: "Kansas" must never agree with
    "Kansas City". Name variance beyond spelling (exonyms, administrative
    dress) is judged by the provider instead — see `_verified`.
    """
    a, b = _slugify(asked), _slugify(returned)
    return bool(a) and a == b


def _verified(asked: str, unit: GeoLookupUnit) -> bool:
    """The round-trip verification (ADR-126), from provider data.

    A unit verifies when the name matches, or when the provider says the
    query was an *exact* match — which is how an exonym or an
    administratively-dressed spelling resolves ("Krung Thep Maha Nakhon" is
    an exact match for Bangkok) while a typo or a wrong-entity fuzzy match
    arrives flagged `partial_match` and is refused. Provider signal, not
    word lists.
    """
    return _names_agree(asked, unit.name) or not unit.partial_match


def _primary_kind(unit: GeoLookupUnit) -> str:
    """The provider's leading type for the unit, `political` filtered out —
    `political` decorates nearly every unit and names none."""
    for t in unit.types:
        if t != "political":
            return t
    return unit.types[0] if unit.types else "unknown"


class GeoRegistry:
    """The single authority on geo identity — resolve, mint, display."""

    def __init__(
        self,
        repo: GeoAreaRepository,
        lookup: GeoLookupProtocol,
        instructor_client: InstructorClient | None = None,
    ) -> None:
        self._repo = repo
        self._lookup = lookup
        self._instructor_client = instructor_client

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

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
        """Resolve a stored location triple to its deepest registered key.

        Returns the deepest identity that resolved — an unknown neighborhood
        on a read path yields the city key, an unknown city the country key.
        None only when there is no country code at all: without it nothing
        can be keyed, which is the `elsewhere` bucket.
        """
        if not country_code:
            return None
        cc = country_code.strip().lower()
        if len(cc) != 2 or not cc.isalpha():
            return None
        if not city:
            return ResolvedAreaKey(geo_key=cc)

        city_row = await self._resolve_city(cc, city, mint=mint)
        if city_row is None:
            return ResolvedAreaKey(geo_key=cc)
        if not neighborhood:
            return ResolvedAreaKey(geo_key=city_row.geo_key, city=city_row)

        area_row = await self._resolve_area(
            cc, city_row, neighborhood, lat=lat, lng=lng, mint=mint
        )
        if area_row is None:
            return ResolvedAreaKey(geo_key=city_row.geo_key, city=city_row)
        return ResolvedAreaKey(geo_key=area_row.geo_key, city=city_row, area=area_row)

    async def rows_for_keys(self, geo_keys: list[str]) -> dict[str, GeoArea]:
        return await self._repo.get_by_keys(geo_keys)

    async def row_for_key(self, geo_key: str) -> GeoArea | None:
        return await self._repo.get_by_key(geo_key)

    async def row_for_legacy_key(self, legacy_key: str) -> GeoArea | None:
        return await self._repo.get_by_legacy_key(legacy_key)

    async def resolve_country(self, name: str, *, mint: bool = True) -> GeoArea | None:
        """Resolve a country *name* to its registered row (geo_key = its ISO
        code). The one lookup with no country constraint — the code is what's
        being resolved. A literal alpha-2 code is not accepted here; callers
        that hold a code already hold the key.
        """
        slug = _slugify(name)
        if not slug:
            return None
        row = await self._repo.lookup_country_alias(slug)
        if row is not None:
            return row
        if not mint:
            return None
        unit = await self._forward(name, None)
        if (
            unit is None
            or "country" not in unit.types
            or not unit.country_code
            or not _verified(name, unit)
        ):
            logger.info("geo_mint_refused", extra={"slot": "country", "asked": name})
            return None
        row = await self._repo.upsert(
            GeoArea(
                place_id=unit.place_id,
                country_code=unit.country_code,
                slot="country",
                kind="country",
                name=unit.name,
                geo_key=unit.country_code,
                lat=unit.lat,
                lng=unit.lng,
                viewport=unit.viewport,
            )
        )
        await self._add_aliases(
            unit.country_code, _COUNTRY_SCOPE, [slug, _slugify(row.name)], row
        )
        return row

    async def resolve_city_global(
        self, name: str, *, mint: bool = True
    ) -> GeoArea | None:
        """Resolve a bare city name with no country context.

        One unconstrained lookup discovers the country (prominence-ranked by
        the provider), then the ordinary constrained city resolution — with
        its round-trip verification — takes over. "Tokyo" resolves; a name
        the provider can't pin to a verified unit is None, never a guess.
        """
        if not mint:
            return None
        unit = await self._forward(name, None)
        if unit is None or not unit.country_code or not _verified(name, unit):
            return None
        return await self._resolve_city(unit.country_code, unit.name, mint=True)

    async def display_row(self, row: GeoArea) -> GeoArea:
        """The row an entity or heading should present — the display group
        when one is minted (a Tibubeneng save presents as Canggu), else the
        row itself. One hop only; groups never chain."""
        if row.groups_into is None:
            return row
        group = await self._repo.get(row.groups_into)
        return group if group is not None else row

    # ------------------------------------------------------------------
    # City slot
    # ------------------------------------------------------------------

    async def _resolve_city(self, cc: str, name: str, *, mint: bool) -> GeoArea | None:
        slug = _slugify(name)
        if not slug:
            return None
        row = await self._repo.lookup_alias(cc, _CITY_SCOPE, slug)
        if row is not None:
            return row
        if not mint:
            return None

        unit = await self._forward(name, cc)
        if unit is None or not _verified(name, unit):
            logger.info(
                "geo_mint_refused",
                extra={"slot": "city", "cc": cc, "asked": name},
            )
            return None
        if unit.country_code and unit.country_code != cc:
            logger.warning(
                "geo_mint_country_mismatch",
                extra={"asked": name, "cc": cc, "got": unit.country_code},
            )
            return None

        row = await self._repo.upsert(
            GeoArea(
                place_id=unit.place_id,
                country_code=cc,
                slot="city",
                kind=_primary_kind(unit),
                name=unit.name,
                geo_key=f"{cc}/{unit.place_id}",
                lat=unit.lat,
                lng=unit.lng,
                viewport=unit.viewport,
            )
        )
        await self._add_aliases(cc, _CITY_SCOPE, [slug, _slugify(row.name)], row)
        return await self._colloquial_pass(row, city_row=None)

    # ------------------------------------------------------------------
    # Area slot
    # ------------------------------------------------------------------

    async def _resolve_area(
        self,
        cc: str,
        city_row: GeoArea,
        name: str,
        *,
        lat: float | None,
        lng: float | None,
        mint: bool,
    ) -> GeoArea | None:
        slug = _slugify(name)
        if not slug:
            return None
        row = await self._repo.lookup_alias(cc, city_row.place_id, slug)
        if row is not None:
            return await self._finalize_area(row, lat=lat, lng=lng)
        if not mint:
            return None

        unit = await self._forward(f"{name}, {city_row.name}", cc)
        if unit is None or not _verified(name, unit):
            logger.info(
                "geo_mint_refused",
                extra={"slot": "area", "cc": cc, "asked": name},
            )
            return None
        if unit.place_id == city_row.place_id:
            # The "neighborhood" is the city itself under another name —
            # record the alias so the next ask resolves without a call,
            # and key at city depth.
            await self._add_aliases(cc, city_row.place_id, [slug], city_row)
            return None

        row = await self._repo.upsert(
            GeoArea(
                place_id=unit.place_id,
                country_code=cc,
                slot="area",
                kind=_primary_kind(unit),
                name=unit.name,
                city_place_id=city_row.place_id,
                geo_key=f"{cc}/{city_row.place_id}/{unit.place_id}",
                lat=unit.lat,
                lng=unit.lng,
                viewport=unit.viewport,
            )
        )
        await self._add_aliases(cc, city_row.place_id, [slug, _slugify(row.name)], row)
        row = await self._colloquial_pass(row, city_row=city_row)
        return await self._finalize_area(row, lat=lat, lng=lng)

    async def _finalize_area(
        self, row: GeoArea, *, lat: float | None, lng: float | None
    ) -> GeoArea:
        """The unit an ask actually keys under: split by point, then folded
        into its display group.

        The fold happens at identity level on purpose — everything that
        stores or reads a key (saves, claims, library grouping, screens)
        must land on ONE key per colloquial area, or the split this whole
        registry exists to end reappears between sibling ids. A split row
        (Gili Air) never folds: it is its own destination.
        """
        row = await self._disambiguate(row, lat=lat, lng=lng)
        if row.groups_into is None or row.split_of is not None:
            return row
        return await self.display_row(row)

    async def _disambiguate(
        self, row: GeoArea, *, lat: float | None, lng: float | None
    ) -> GeoArea:
        """Resolve an ambiguous unit to the split its point falls in.

        Pure geometry against rows minted with the unit — no network, no
        lists. Without coordinates (or outside every split) the unit itself
        is the honest answer.
        """
        if not row.ambiguous or lat is None or lng is None:
            return row
        splits = await self._repo.get_splits(row.place_id)
        containing = [s for s in splits if s.contains(lat, lng)]
        if not containing:
            return row
        return min(containing, key=_viewport_size)

    # ------------------------------------------------------------------
    # Minting internals
    # ------------------------------------------------------------------

    async def _forward(self, query: str, cc: str | None) -> GeoLookupUnit | None:
        try:
            return await self._lookup.forward(query=query, country_code=cc)
        except GeoLookupError:
            # Mint later; the write degrades to a coarser key today.
            logger.warning("geo_mint_lookup_failed", extra={"query": query})
            return None

    async def _add_aliases(
        self, cc: str, scope: str, slugs: list[str], row: GeoArea
    ) -> None:
        for slug in dict.fromkeys(s for s in slugs if s):
            await self._repo.add_alias(cc, scope, slug, row.place_id)

    async def _colloquial_pass(
        self, row: GeoArea, *, city_row: GeoArea | None
    ) -> GeoArea:
        """Mint the colloquial layer for a fresh row, best-effort.

        Failures leave the row exactly as the geocoder described it — the
        layer is decoration on verified identity, never a gate on it.
        """
        if self._instructor_client is None:
            return row
        try:
            response = await self._infer_colloquial(row, city_row)
        except Exception:
            logger.warning(
                "geo_colloquial_pass_failed",
                extra={"place_id": row.place_id},
                exc_info=True,
            )
            return row

        updated = row
        colloquial = _clean_name(response.colloquial_name)
        if colloquial and _slugify(colloquial) != _slugify(row.name):
            updated = updated.model_copy(update={"colloquial_name": colloquial})

        group = await self._verify_group(response.part_of, updated, city_row)
        if group is not None:
            updated = updated.model_copy(update={"groups_into": group.place_id})

        if updated != row:
            updated = await self._repo.upsert(updated)

        await self._mint_splits(updated, city_row, response.covers)
        return await self._repo.get(updated.place_id) or updated

    async def _verify_group(
        self,
        part_of: str | None,
        row: GeoArea,
        city_row: GeoArea | None,
    ) -> GeoArea | None:
        """Verify a claimed containing area and return its row, or None.

        The group must geocode to a real, distinct unit in the same scope.
        Its own row is minted *without* a colloquial pass — groups don't
        chain, so the one hop `display_row` takes is always terminal.
        """
        name = _clean_name(part_of)
        if not name or city_row is None or row.slot != "area":
            return None
        # The prompt says "not the city itself"; this is the code backstop.
        # Without it, "Canggu is part of Bali" verifies by geocoding
        # "Bali, Bali" into whatever same-named unit answers, and the fold
        # reroutes a perfectly good area into it.
        if _names_agree(name, city_row.name) or (
            city_row.colloquial_name and _names_agree(name, city_row.colloquial_name)
        ):
            return None
        cc = row.country_code
        slug = _slugify(name)
        existing = await self._repo.lookup_alias(cc, city_row.place_id, slug)
        if existing is not None:
            return existing if existing.place_id != row.place_id else None

        unit = await self._forward(f"{name}, {city_row.name}", cc)
        if (
            unit is None
            or not _verified(name, unit)
            or unit.place_id in (row.place_id, city_row.place_id)
        ):
            return None
        group = await self._repo.upsert(
            GeoArea(
                place_id=unit.place_id,
                country_code=cc,
                slot="area",
                kind=_primary_kind(unit),
                name=unit.name,
                city_place_id=city_row.place_id,
                geo_key=f"{cc}/{city_row.place_id}/{unit.place_id}",
                lat=unit.lat,
                lng=unit.lng,
                viewport=unit.viewport,
            )
        )
        await self._add_aliases(
            cc, city_row.place_id, [slug, _slugify(group.name)], group
        )
        return group

    async def _mint_splits(
        self, row: GeoArea, city_row: GeoArea | None, covers: list[str]
    ) -> None:
        """Mint the distinct places one admin name covers (the Gili class).

        Every candidate must geocode to its own unit sitting inside the
        parent's viewport; the set is stored only when at least two members
        verify, and the parent is then marked ambiguous so per-point
        resolution kicks in. Anything less keeps the honest coarse unit.
        """
        names = [n for n in (_clean_name(c) for c in covers) if n]
        if len(names) < _MIN_SPLITS or city_row is None or row.slot != "area":
            return
        minted: list[GeoArea] = []
        for name in names[:_MAX_SPLITS]:
            unit = await self._forward(f"{name}, {city_row.name}", row.country_code)
            if (
                unit is None
                or not _verified(name, unit)
                or unit.place_id in (row.place_id, city_row.place_id)
                or not row.contains(
                    unit.lat, unit.lng, margin=_SPLIT_CONTAINMENT_MARGIN
                )
            ):
                continue
            minted.append(
                GeoArea(
                    place_id=unit.place_id,
                    country_code=row.country_code,
                    slot="area",
                    kind=_primary_kind(unit),
                    name=unit.name,
                    split_of=row.place_id,
                    city_place_id=city_row.place_id,
                    geo_key=(f"{row.country_code}/{city_row.place_id}/{unit.place_id}"),
                    lat=unit.lat,
                    lng=unit.lng,
                    viewport=unit.viewport,
                )
            )
        if len(minted) < _MIN_SPLITS:
            if names:
                logger.info(
                    "geo_splits_discarded",
                    extra={"place_id": row.place_id, "offered": len(names)},
                )
            return
        for split in minted:
            stored = await self._repo.upsert(split)
            await self._add_aliases(
                row.country_code, city_row.place_id, [_slugify(stored.name)], stored
            )
        await self._repo.set_ambiguous(row.place_id)

    async def _infer_colloquial(
        self, row: GeoArea, city_row: GeoArea | None
    ) -> _MintResponse:
        context = f"country code: {row.country_code}\n"
        if city_row is not None:
            context += f"inside: {city_row.name}\n"
        user_content = (
            f"unit name (geocoder, English): {row.name}\n"
            f"unit type: {row.kind}\n"
            f"{context}"
        )
        async with traced_call(
            "geo.registry",
            "geo_colloquial_mint",
            role="area_registry",
            extra={"place_id": row.place_id},
            standalone=True,
        ) as t:
            try:
                response = cast(
                    _MintResponse,
                    await self._instructor_client.extract(  # type: ignore[union-attr]
                        response_model=_MintResponse,
                        messages=[
                            {
                                "role": "system",
                                "content": get_prompt("area_registry"),
                            },
                            {"role": "user", "content": user_content},
                        ],
                    ),
                )
            except Exception as exc:
                t.fail(exc)
                raise
            t.output = {
                "colloquial": bool(response.colloquial_name),
                "part_of": bool(response.part_of),
                "covers": len(response.covers),
            }
        return response


def _clean_name(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > _MAX_NAME_LEN:
        return None
    return cleaned


def _viewport_size(row: GeoArea) -> float:
    if row.viewport is None or len(row.viewport) != 4:
        return float("inf")
    south, west, north, east = row.viewport
    return abs(north - south) * abs(east - west)
