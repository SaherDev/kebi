"""AreaService — the read-through resolver over the area entity store.

One notion of "an area" for every subsystem (location-kinds Step 2, widened to
every granularity in Step 6): store first, geocode on miss, persist what
verifies. Generalizes the ADR-126 recipe — a lookup is accepted only when it
round-trips (the returned feature slug-matches the asked-for name, inside the
expected country) and refused otherwise; a refused name is never substituted
with a nearby or similar entity.

**One engine, one spec per kind.** There is a single resolution algorithm
(`_resolve`) and a declarative `AreaKindSpec` for each kind, saying which
provider feature types are that kind, which response fields name it, and where
its key sits in the hierarchy. Adding a kind is adding a spec — never a new
resolver method and never a branch inside an existing one. The public
`resolve_*` methods differ only in which kinds they will accept, which is what
lets `resolve_city` stay settlement-only (the corridor endpoint resolver
depends on that narrowness) while `resolve_area` accepts every kind but
country — see `ALL_AREA_KINDS` for why country is excluded rather than free.

**Verification is a round trip, not equality.** The provider routinely answers
with the fuller local name of the feature asked for — "My Khe" comes back as
*My Khe Beach*, "Hai Van Pass" as *Hải Vân Pass* — so a returned name that
wraps the asked-for one is accepted. The containment is one-directional and
word-anchored, which is exactly what keeps "Ha Giang Loop" from matching "Ha
Giang": naming a place and naming a trip through it are different, and the
direction of containment is the difference.

Geometry compliance: coordinates are provider content and expire after
30 days; a store hit with stale geometry re-geocodes through the row's
stored place ID (which is storable indefinitely) before returning.
Best-effort — a failed refresh serves the stored geometry and retries
on the next read.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from kebi.core.areas.models import AreaContext, AreaEntity, AreaKind
from kebi.core.knowledge.schemas import (
    _slugify,
    build_geo_key,
    slugs_match,
)

if TYPE_CHECKING:
    from kebi.db.repositories.area_entity_repository import AreaEntityRepository
    from kebi.providers.geocoding import GeocodeResult, GeocoderProtocol

logger = logging.getLogger(__name__)

# Provider ToS: geocoded coordinates may be cached at most 30 days.
_GEOMETRY_MAX_AGE = timedelta(days=30)

# Where a kind's entity key sits in the `build_geo_key` hierarchy:
#   country  -> "vn"
#   city     -> "vn/hoi-an"          (a region keys at the same depth)
#   sub_city -> "vn/da-nang/an-thuong"
KeyDepth = Literal["country", "city", "sub_city"]


def _wraps(asked_slug: str, found_slug: str) -> bool:
    """True when `found_slug` is `asked_slug` plus extra whole words.

    Word-boundary containment, not substring: "hai-van-pass" wraps into
    "deo-hai-van-pass", but "an" must never match "an-thuong" or every short
    name would resolve to something larger that happens to contain it.
    """
    if not asked_slug or not found_slug or asked_slug == found_slug:
        return False
    asked_words = asked_slug.split("-")
    found_words = found_slug.split("-")
    if len(asked_words) > len(found_words):
        return False
    return any(
        found_words[i : i + len(asked_words)] == asked_words
        for i in range(len(found_words) - len(asked_words) + 1)
    )


def _honours_hint(
    spec: AreaKindSpec, result: GeocodeResult, city_hint: str | None
) -> bool:
    """Whether a sub-city match actually sits in the city the caller named.

    The bare-name fallback is what makes ambiguous sub-city names resolvable,
    and it is also how they go wrong: asking for "An Thuong" near Da Nang
    found a same-named neighborhood in Nho Quan, 500 km away, and answered a
    question about the wrong end of the country. A caller who supplied a city
    is asserting where the area is.

    **Only neighborhoods are bound to the hint.** A neighborhood is by
    definition inside one city, so a city mismatch means a different place of
    the same name. Geography is not: Hai Van Pass runs over the Da Nang / Hue
    boundary and the provider files it under Hue, so binding it to the asked
    city would refuse the very feature that was asked for. Cities and regions
    are exempt for the obvious reason that they *are* the hint.
    """
    if not city_hint or spec.kind != "neighborhood":
        return True
    return bool(result.city) and slugs_match(city_hint, result.city)


@dataclass(frozen=True)
class AreaKindSpec:
    """Everything the resolver needs to know about one kind of area.

    `provider_types` are the geocoder classifications that mean this kind.
    `name_fields` are the `GeocodeResult` attributes that name **the feature
    itself** — the round-trip check reads them in order, and getting this
    wrong in the permissive direction is how a route name would match its
    containing city and be stored as that city. A pass or a street is named
    only by `name`; its `city` describes what contains it, not what it is.

    `verify_name=False` is for the country spec alone: a result the geocoder
    types `country` inside the asked country is already verified by its type,
    and slug-matching display names would refuse legitimate spellings
    ("Viet Nam" against "Vietnam") for no gain.
    """

    kind: AreaKind
    provider_types: frozenset[str]
    name_fields: tuple[str, ...]
    depth: KeyDepth
    verify_name: bool = True

    def canonical_name(self, asked: str, result: GeocodeResult) -> str | None:
        """The feature's canonical name, or None when it isn't what was asked.

        An exact slug match wins outright. Failing that, a returned name that
        **wraps** the asked-for one is accepted — "My Khe" against *My Khe
        Beach*, "Hai Van Pass" against *Hải Vân Pass*. The provider routinely
        answers with the fuller local name of the very feature asked for, and
        a strict equality check refuses exactly the sub-city areas this step
        exists to resolve.

        The wrapping allowance is deliberately one-directional and anchored:
        the asked-for slug must appear *whole* in the returned one. "Ha Giang
        Loop" is not contained in "Ha Giang", so a journey still does not
        round-trip — the containment runs the other way, which is the whole
        difference between naming a place and naming a trip through it.
        """
        if not self.verify_name:
            for field in self.name_fields:
                value = getattr(result, field, None)
                if value:
                    return str(value)
            return asked

        asked_slug = _slugify(asked)
        wrapping: str | None = None
        for field in self.name_fields:
            value = getattr(result, field, None)
            if not value:
                continue
            text = str(value)
            if slugs_match(asked, text):
                return text
            if wrapping is None and asked_slug and _wraps(asked_slug, _slugify(text)):
                wrapping = text
        return wrapping


# The registry. A new kind is a new entry here and nothing else.
#
# What is deliberately absent is a route spec. A named journey with no
# verifiable footprint is not geography, however route-shaped the words are —
# it fails to resolve and collapses to its containing area (roadmap:
# external routes are untrusted). The line is the footprint, not the noun.
AREA_KIND_SPECS: tuple[AreaKindSpec, ...] = (
    AreaKindSpec(
        kind="country",
        provider_types=frozenset({"country"}),
        name_fields=("country", "name"),
        depth="country",
        verify_name=False,
    ),
    AreaKindSpec(
        kind="city",
        provider_types=frozenset({"locality", "postal_town"}),
        name_fields=("city", "name"),
        depth="city",
    ),
    AreaKindSpec(
        # Provinces and states. ADR-124 already classifies these at city scope
        # for radius purposes; as *entities* they are their own kind, and they
        # key at the same depth as a city because that is where their claims
        # already live.
        kind="region",
        provider_types=frozenset(
            {"administrative_area_level_1", "administrative_area_level_2"}
        ),
        name_fields=("city", "name"),
        depth="city",
    ),
    AreaKindSpec(
        # The granularity "where should I stay?" actually asks about.
        kind="neighborhood",
        provider_types=frozenset(
            {
                "sublocality",
                "sublocality_level_1",
                "sublocality_level_2",
                "neighborhood",
            }
        ),
        name_fields=("neighborhood", "name"),
        depth="sub_city",
    ),
    AreaKindSpec(
        # Geography with an extent — a pass, a beach, a lagoon, a park.
        # Modelled as areas so they can be answers, and so they can never
        # become venue rows: no type-based save guard could separate Hai Van
        # Pass from Lang Co Beach, because the geocoder types them alike.
        kind="natural_feature",
        provider_types=frozenset({"natural_feature", "park", "beach"}),
        name_fields=("name",),
        depth="sub_city",
    ),
    AreaKindSpec(
        kind="street",
        provider_types=frozenset({"route", "street_address"}),
        name_fields=("name",),
        depth="sub_city",
    ),
)

_SPEC_BY_PROVIDER_TYPE: dict[str, AreaKindSpec] = {
    provider_type: spec
    for spec in AREA_KIND_SPECS
    for provider_type in spec.provider_types
}

_SPEC_BY_KIND: dict[AreaKind, AreaKindSpec] = {
    spec.kind: spec for spec in AREA_KIND_SPECS
}

# Kinds accepted by each public entry point. `resolve_city` stays deliberately
# narrow: the corridor resolver reuses it to tell a real settlement from a
# fuzzy establishment match (ADR-136), and widening it would let a corridor
# endpoint resolve to a street.
SETTLEMENT_KINDS: frozenset[AreaKind] = frozenset({"city", "region"})

# Every kind `resolve_area` will accept — deliberately **excluding country**.
#
# A country result is verified by its feature type alone (its display name is
# not slug-checked, so alpha-2 codes and spellings like "Viet Nam" resolve),
# which makes it a catch-all: the provider answers almost any unresolvable
# query inside a country with that country, and accepting it here turned
# "Ha Giang Loop" into `vn` — the exact opposite of the locked rule that a
# named journey resolves to nothing. Countries keep their own entry point,
# `resolve_country`, where asking for one is explicit.
ALL_AREA_KINDS: frozenset[AreaKind] = frozenset(
    spec.kind for spec in AREA_KIND_SPECS if spec.kind != "country"
)

# Google result types accepted as a settlement. Public because the corridor
# resolver reuses it directly (ADR-136) — widening it there, not here, since a
# corridor endpoint is only a coordinate while an entity is an identity.
CITY_LEVEL_TYPES = frozenset(
    provider_type
    for kind in SETTLEMENT_KINDS
    for provider_type in _SPEC_BY_KIND[kind].provider_types
)


class AreaService:
    """Store-first area resolution, verified-or-refuse (ADR-126)."""

    def __init__(
        self,
        repo: AreaEntityRepository,
        geocoder: GeocoderProtocol,
    ) -> None:
        self._repo = repo
        self._geocoder = geocoder
        # Per-instance memo (one instance per request/harvest): repeated
        # names skip even the store read. The DB is the durable memo.
        self._memo: dict[tuple[str, str], AreaEntity | None] = {}

    async def get(self, entity_key: str) -> AreaEntity | None:
        """Store-only read (+ compliance geometry refresh when stale)."""
        entity = await self._repo.get(entity_key)
        if entity is None:
            return None
        return await self._fresh(entity)

    async def find_known_by_slug(self, name_slugs: list[str]) -> dict[str, AreaEntity]:
        """Store-only: which of these name slugs are already known areas.

        No geocode on a miss — a miss here means "not known to be an area",
        which is a different and cheaper question than "resolve this name".
        """
        return await self._repo.find_by_name_slugs(name_slugs)

    async def resolve_country(self, name: str) -> AreaEntity | None:
        """Resolve a country name (or literal alpha-2 code) to its entity.

        Verified by feature type: the geocoder match must *be* a country,
        not a street or venue named after one.
        """
        asked = name.strip()
        memo_key = ("country", _slugify(asked))
        if memo_key in self._memo:
            return self._memo[memo_key]

        # A literal alpha-2 code is its own key, so a stored country answers
        # without a geocode — and the code doubles as the region bias.
        cc = asked.lower() if len(asked) == 2 and asked.isalpha() else None
        if cc is not None:
            stored = await self._repo.get(build_geo_key(cc))
            if stored is not None:
                fresh = await self._fresh(stored)
                self._memo[memo_key] = fresh
                return fresh

        entity: AreaEntity | None = await self._resolve(
            asked, scope_cc=cc, allowed=frozenset({"country"}), city_hint=None
        )
        self._memo[memo_key] = entity
        return entity

    async def resolve_city(self, name: str, country_code: str) -> AreaEntity | None:
        """Resolve a city/province name within a country, round-trip verified.

        Settlements only — a name that resolves to a street or a natural
        feature is refused here even though `resolve_area` would accept it.
        Refusal returns `None`; the caller clarifies or drops, never
        substitutes.
        """
        return await self._resolve_scoped(
            name, country_code, allowed=SETTLEMENT_KINDS, city_hint=None, tag="city"
        )

    async def resolve_area(
        self, name: str, country_code: str, *, city_hint: str | None = None
    ) -> AreaEntity | None:
        """Resolve a named area of any kind within a country (Step 6).

        Same verification as `resolve_city`, wider acceptance: regions,
        neighborhoods, natural features and named streets all resolve here.

        `city_hint` qualifies a sub-city name that would otherwise be
        ambiguous ("An Thuong" alone matches little; "An Thuong, Da Nang"
        resolves). It only shapes the *query* — the round-trip check still
        runs against the name the caller asked for, so a hint can never
        smuggle in a match.
        """
        return await self._resolve_scoped(
            name,
            country_code,
            allowed=ALL_AREA_KINDS,
            city_hint=city_hint,
            tag="area",
        )

    async def resolve_noted_name(
        self, name: str, context: AreaContext, *, probe_name: bool = True
    ) -> AreaEntity | None:
        """Resolve a noted non-venue name to the entity its interest belongs to.

        The subject-vs-container rule: the name itself is tried as an area
        first ("Hoi An", "Mui Ne" — administrative rejections resolve to
        themselves); a name that refuses (routes — "Ha Giang Loop" never
        slug-matches a locality) collapses to its *containing* area from
        the share's context (city, else country), per the roadmap's
        external-routes-are-untrusted rule. Callers that already know the
        name is a route (`non_venue_route` detections) pass
        `probe_name=False` to skip the doomed name-as-area geocode.
        """
        codes = await self._context_country_codes(context)
        if probe_name:
            for cc in codes:
                entity = await self.resolve_city(name, cc)
                if entity is not None:
                    return entity
        if context.city:
            for cc in codes:
                entity = await self.resolve_city(context.city, cc)
                if entity is not None:
                    return entity
        if codes:
            return await self.resolve_country(codes[0])
        return None

    # ---- the one resolution engine ---------------------------------------

    async def _resolve_scoped(
        self,
        name: str,
        country_code: str,
        *,
        allowed: frozenset[AreaKind],
        city_hint: str | None,
        tag: str,
    ) -> AreaEntity | None:
        """Memoized country-scoped resolve. The memo is per instance (one per
        request or harvest pass); the store is the durable one."""
        cc = country_code.strip().lower()
        asked = name.strip()
        memo_key = (tag, f"{cc}:{_slugify(city_hint or '')}:{_slugify(asked)}")
        if memo_key in self._memo:
            return self._memo[memo_key]
        entity = await self._resolve(
            asked, scope_cc=cc, allowed=allowed, city_hint=city_hint
        )
        self._memo[memo_key] = entity
        return entity

    async def _resolve(
        self,
        asked: str,
        *,
        scope_cc: str | None,
        allowed: frozenset[AreaKind],
        city_hint: str | None,
    ) -> AreaEntity | None:
        """Store, then alias, then one geocode verified against a kind spec.

        The single path every kind takes. What varies between callers is only
        `allowed` — which kinds they are willing to accept — so a widening
        never touches this method.
        """
        if not asked:
            return None

        if scope_cc is not None:
            for key in self._candidate_keys(asked, scope_cc, city_hint, allowed):
                stored = await self._repo.get(key)
                if stored is not None and stored.entity_type in allowed:
                    return await self._fresh(stored)
            alias_hit = await self._repo.find_by_alias(scope_cc, _slugify(asked))
            if (
                alias_hit is not None
                and alias_hit.entity_type in allowed
                and self._alias_honours_hint(alias_hit, scope_cc, city_hint)
            ):
                return await self._fresh(alias_hit)

        verified = await self._first_verified(asked, scope_cc, city_hint, allowed)
        if verified is None:
            return None
        spec, result, canonical = verified
        cc = result.country_code or ""
        entity_key, parent_key = self._build_keys(spec, cc, canonical, result)
        if entity_key is None:
            return None

        # A country asked for by name may already be stored under the code the
        # geocode just revealed — return that row rather than re-persisting.
        if spec.depth == "country":
            stored = await self._repo.get(entity_key)
            if stored is not None:
                return await self._fresh(stored)

        return await self._persist(
            entity_key=entity_key,
            entity_type=spec.kind,
            display_name=canonical,
            country_code=cc,
            asked_name=asked,
            result=result,
            parent_key=parent_key,
        )

    @staticmethod
    def _alias_honours_hint(
        hit: AreaEntity, scope_cc: str, city_hint: str | None
    ) -> bool:
        """Whether a stored alias match sits in the city the caller named.

        Alias lookup is country-scoped, which is right for a city ("Saigon" is
        unique in Vietnam) and wrong for a neighborhood name that repeats:
        once `An Thuong` in Nho Quan was in the store, asking for `An Thuong`
        near Da Nang matched it by alias and skipped the geocode entirely —
        the store turning one bad resolution into a permanent one. Mirrors
        `_honours_hint`, including its scope: only neighborhoods are bound to
        the asked city, because only they are definitionally inside one.
        """
        if not city_hint or hit.entity_type != "neighborhood" or hit.parent_key is None:
            return True
        expected = build_geo_key(scope_cc, city_hint)
        return hit.parent_key == expected or hit.parent_key == build_geo_key(scope_cc)

    async def _first_verified(
        self,
        asked: str,
        scope_cc: str | None,
        city_hint: str | None,
        allowed: frozenset[AreaKind],
    ) -> tuple[AreaKindSpec, GeocodeResult, str] | None:
        """Geocode until something round-trips, hinted query first.

        A bare sub-city name is often ambiguous on its own ("An Thuong"), so
        the city hint rides the query — but the hint can also make the answer
        *worse*: "Hai Van Pass, Da Nang" returns the road under its Vietnamese
        name (*Đèo Hải Vân*), which does not round-trip, while the bare name
        returns the feature itself. So the bare name is tried as a fallback
        rather than the hint being trusted to help.

        Cost is one geocode in the normal case and two only when the hinted
        query fails to verify — a call that would otherwise have been a
        refusal, so the extra spend buys an answer rather than padding one.
        The hint never *relaxes* verification: both attempts are checked
        against the name the caller asked for.
        """
        queries = []
        if city_hint and _slugify(city_hint) not in _slugify(asked):
            queries.append(f"{asked}, {city_hint}")
        queries.append(asked)

        for query in queries:
            result = await self._search(query=query, region_code=scope_cc)
            if result is None or not result.country_code:
                continue
            if scope_cc is not None and result.country_code != scope_cc:
                continue
            spec = _SPEC_BY_PROVIDER_TYPE.get(result.place_type or "")
            if spec is None or spec.kind not in allowed:
                continue
            canonical = spec.canonical_name(asked, result)
            if canonical is None:
                continue
            if not _honours_hint(spec, result, city_hint):
                continue
            return spec, result, canonical
        return None

    @staticmethod
    def _build_keys(
        spec: AreaKindSpec, cc: str, canonical: str, result: GeocodeResult
    ) -> tuple[str | None, str | None]:
        """This entity's key and its parent's, from the spec's depth.

        A sub-city feature whose response carries no city falls back to
        city-level depth rather than being dropped: a pass between two
        provinces is still a real, verified area, and keying it under the
        country is truthful where inventing a containing city would not be.
        """
        try:
            if spec.depth == "country":
                return build_geo_key(cc), None
            if spec.depth == "city":
                return build_geo_key(cc, canonical), build_geo_key(cc)
            if result.city:
                return (
                    build_geo_key(cc, result.city, canonical),
                    build_geo_key(cc, result.city),
                )
            return build_geo_key(cc, canonical), build_geo_key(cc)
        except ValueError:
            return None, None

    @staticmethod
    def _candidate_keys(
        asked: str, cc: str, city_hint: str | None, allowed: frozenset[AreaKind]
    ) -> list[str]:
        """Store keys this name could already live under, coarse depth first.

        Both plausible depths are tried because depth is only known after the
        geocode — a region and a city sit at `cc/<slug>`, everything sub-city
        at `cc/<city>/<slug>`. Depths no allowed kind uses are skipped.

        Country depth is absent on purpose: its key is the country code alone
        and carries nothing of the asked-for name, so probing it would return
        the country for *every* name asked inside it. A country resolves
        through its own entry point (literal code) or through the post-geocode
        store check.
        """
        depths = {_SPEC_BY_KIND[kind].depth for kind in allowed}
        keys: list[str] = []
        if "city" in depths:
            with contextlib.suppress(ValueError):
                keys.append(build_geo_key(cc, asked))
        if "sub_city" in depths and city_hint:
            with contextlib.suppress(ValueError):
                keys.append(build_geo_key(cc, city_hint, asked))
        return keys

    async def _persist(
        self,
        *,
        entity_key: str,
        entity_type: str,
        display_name: str,
        country_code: str,
        asked_name: str,
        result: GeocodeResult,
        parent_key: str | None,
    ) -> AreaEntity:
        # The canonical name's own slug is always an alias, not only the
        # variants seen in the wild. That is what lets a store-only lookup
        # answer "is this name already known to be geography?" from the GIN
        # index alone — the check the venue path uses to stop a pass being
        # offered as a savable venue.
        aliases = [_slugify(display_name)]
        asked_slug = _slugify(asked_name)
        if asked_slug and asked_slug not in aliases:
            aliases.append(asked_slug)
        aliases = [slug for slug in aliases if slug]
        entity = AreaEntity(
            entity_key=entity_key,
            entity_type=entity_type,
            name=display_name,
            aliases=aliases,
            country_code=country_code,
            lat=result.lat,
            lng=result.lng,
            bbox=result.bbox,
            place_type=result.place_type,
            parent_key=parent_key,
            provider_id=result.provider_id,
            geo_refreshed_at=datetime.now(UTC),
        )
        return await self._repo.upsert(entity)

    # ---- helpers ---------------------------------------------------------

    async def _context_country_codes(self, context: AreaContext) -> list[str]:
        codes: list[str] = []
        if context.country_code:
            codes.append(context.country_code.strip().lower())
        elif context.country:
            country = await self.resolve_country(context.country)
            if country is not None:
                codes.append(country.country_code)
        return codes

    async def _fresh(self, entity: AreaEntity) -> AreaEntity:
        """Compliance refresh: re-geocode stale geometry through the stored
        place ID. Best-effort — the stored entity is served on failure."""
        age_ok = (
            entity.geo_refreshed_at is not None
            and datetime.now(UTC) - entity.geo_refreshed_at < _GEOMETRY_MAX_AGE
        )
        if age_ok or not entity.provider_id:
            return entity
        try:
            result = await self._geocoder.geocode_place_id(entity.provider_id)
        except Exception as exc:
            logger.warning(
                "area geometry refresh failed for %s: %s", entity.entity_key, exc
            )
            return entity
        if result is None:
            return entity
        now = datetime.now(UTC)
        await self._repo.update_geometry(
            entity.entity_key,
            lat=result.lat,
            lng=result.lng,
            bbox=result.bbox,
            refreshed_at=now,
        )
        return entity.model_copy(
            update={
                "lat": result.lat,
                "lng": result.lng,
                "bbox": result.bbox,
                "geo_refreshed_at": now,
            }
        )

    async def _search(
        self, *, query: str, region_code: str | None
    ) -> GeocodeResult | None:
        try:
            return await self._geocoder.search_area(
                query=query, region_code=region_code
            )
        except Exception as exc:
            logger.warning("area geocode failed for %r: %s", query, exc)
            return None
