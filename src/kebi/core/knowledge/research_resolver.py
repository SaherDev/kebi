"""Staged, verified-or-refuse entity resolution for the research tool.

The Da Nang→Koh Samui bug was an entity-resolution failure: the agent
answered about a different place than the one asked about. This resolver
guarantees that can't happen on the research path — the key it returns is
always derived from the *asked-about* entity, verified, or it returns a
clarification instead. It never substitutes a nearby or stale entity.

Geo-only (country / city / neighborhood): venue research has no `place:<id>`
path in v1 — a venue question resolves to its area or clarifies.

Stages:

1. **Exact match (confidence 1.0)** — the named area slug-matches the turn's
   already-resolved `working_location` (ADR-124's authoritative resolution),
   so its verified country code keys the entity with no geocode call. If the
   named area does NOT match, the working location is *not* used — that
   refusal is what closes the wrong-entity swap.
2. **Verified registry resolve (confidence 0.8)** — resolve the name
   through the geo registry (alias hit, or a round-trip-verified mint),
   constrained to a country: the agent-passed country first (the
   orchestrator usually knows which country the asked-about city is in — a
   stale working location's country alone would wrongly fail a
   cross-country question), then the working location's.
3. **Clarify** — no country context at all (`ambiguous`) or a name that
   won't verify (`unresolved`): a `ResolvedEntity` with
   `needs_clarification=True` and a plain-language reason. Never a key for a
   different entity.

A named neighborhood that the registry can't verify degrades the read to
its verified city's key — the city/country ancestors still answer, and no
key is ever minted for an unverified name.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from kebi.core.knowledge.schemas import EntityType, slugs_match

if TYPE_CHECKING:
    from kebi.core.agent.location import WorkingLocation
    from kebi.core.geo.protocols import GeoRegistryProtocol

logger = logging.getLogger(__name__)

_EXACT_CONFIDENCE = 1.0
_GEOCODED_CONFIDENCE = 0.8

_ALPHA2_RE = re.compile(r"^[A-Za-z]{2}$")


class ResolvedEntity(BaseModel):
    """The resolver's verdict: a verified entity key with a confidence, or a
    clarification. `empty_reason` distinguishes "no country context to even
    constrain the lookup" (`ambiguous`) from "had context, wouldn't verify"
    (`unresolved`) so the agent can ask the right question."""

    model_config = ConfigDict(frozen=True)

    entity_key: str | None = None
    entity_type: EntityType | None = None
    entity_name: str | None = None
    confidence: float = 0.0
    needs_clarification: bool = False
    clarification_reason: str | None = None
    empty_reason: Literal["unresolved", "ambiguous"] | None = None


def _clarify(
    reason: str, *, empty: Literal["unresolved", "ambiguous"]
) -> ResolvedEntity:
    return ResolvedEntity(
        needs_clarification=True, clarification_reason=reason, empty_reason=empty
    )


class ResearchEntityResolver:
    """Resolve the asked-about area to a verified entity key, or refuse."""

    def __init__(
        self, geo_registry: GeoRegistryProtocol, *, confidence_min: float = 0.5
    ) -> None:
        self._registry = geo_registry
        self._confidence_min = confidence_min

    async def resolve(
        self,
        *,
        city: str | None = None,
        country: str | None = None,
        neighborhood: str | None = None,
        working_location: WorkingLocation | None = None,
    ) -> ResolvedEntity:
        """The most specific agent-named area wins (neighborhood > city >
        country); with no named area the turn's working location is the
        asked-about entity ("around here"), at city scope."""
        if city or neighborhood:
            entity = await self._resolve_city_scope(
                city=city,
                neighborhood=neighborhood,
                country=country,
                wl=working_location,
            )
        elif country:
            entity = await self._resolve_country_scope(country, working_location)
        else:
            entity = await self._resolve_here(working_location)
        if not entity.needs_clarification and entity.confidence < self._confidence_min:
            name = entity.entity_name or "that place"
            return _clarify(
                f"could not pin down {name} confidently enough to answer about it",
                empty="unresolved",
            )
        return entity

    # ---- scopes ----------------------------------------------------------

    async def _resolve_here(self, wl: WorkingLocation | None) -> ResolvedEntity:
        if wl is None:
            return _clarify(
                "no place in play — need a city, country, or neighborhood to research",
                empty="ambiguous",
            )
        code = await self._working_country_code(wl)
        if code is None:
            return _clarify(
                f"could not verify the current location ({wl.city})",
                empty="unresolved",
            )
        return await self._geo_entity(code, wl.city, None, confidence=_EXACT_CONFIDENCE)

    async def _resolve_country_scope(
        self, country: str, wl: WorkingLocation | None
    ) -> ResolvedEntity:
        # Stage 1 — the named country is the working location's own.
        if wl is not None and slugs_match(country, wl.country):
            code = await self._working_country_code(wl)
            if code is not None:
                return _country_entity(code, wl.country, confidence=_EXACT_CONFIDENCE)
        # Stage 2 — verified registry resolve by name (or a literal code).
        if _ALPHA2_RE.match(country):
            return _country_entity(
                country.lower(), country.upper(), confidence=_GEOCODED_CONFIDENCE
            )
        row = await self._registry.resolve_country(country)
        if row is not None:
            return _country_entity(
                row.country_code, row.display_name, confidence=_GEOCODED_CONFIDENCE
            )
        return _clarify(
            f"could not verify a country called '{country}'", empty="unresolved"
        )

    async def _resolve_city_scope(
        self,
        *,
        city: str | None,
        neighborhood: str | None,
        country: str | None,
        wl: WorkingLocation | None,
    ) -> ResolvedEntity:
        city_name = city or (wl.city if wl is not None else None)
        if not city_name:
            return _clarify(
                f"a neighborhood alone is not enough — which city is "
                f"'{neighborhood}' in?",
                empty="ambiguous",
            )

        # Stage 1 — the named city is the working location's own city (and
        # any named country doesn't contradict it).
        if (
            wl is not None
            and slugs_match(city_name, wl.city)
            and self._country_matches_wl(country, wl)
        ):
            code = await self._working_country_code(wl)
            if code is not None:
                return await self._geo_entity(
                    code, wl.city, neighborhood, confidence=_EXACT_CONFIDENCE
                )

        # Stage 2 — verified registry resolve; agent-passed country first.
        codes = await self._candidate_country_codes(country, wl)
        if not codes:
            return _clarify(
                f"'{city_name}' — which country is that in?", empty="ambiguous"
            )
        for code in codes:
            entity = await self._geo_entity(
                code, city_name, neighborhood, confidence=_GEOCODED_CONFIDENCE
            )
            if not entity.needs_clarification:
                return entity
        return _clarify(
            f"could not verify a city called '{city_name}'"
            + (f" in {country}" if country else ""),
            empty="unresolved",
        )

    # ---- helpers ---------------------------------------------------------

    async def _working_country_code(self, wl: WorkingLocation) -> str | None:
        """The working location's ISO code — carried on the model since it
        gained `country_code`; derived from the display name for states
        checkpointed before that field existed."""
        if wl.country_code:
            return wl.country_code
        row = await self._registry.resolve_country(wl.country)
        return row.country_code if row is not None else None

    async def _candidate_country_codes(
        self, country: str | None, wl: WorkingLocation | None
    ) -> list[str]:
        """Country codes to constrain a city lookup, in trust order:
        agent-passed country first, then the working location's."""
        codes: list[str] = []
        if country:
            if _ALPHA2_RE.match(country):
                codes.append(country.lower())
            else:
                row = await self._registry.resolve_country(country)
                if row is not None:
                    codes.append(row.country_code)
        if wl is not None:
            code = await self._working_country_code(wl)
            if code and code not in codes:
                codes.append(code)
        return codes

    @staticmethod
    def _country_matches_wl(country: str | None, wl: WorkingLocation) -> bool:
        """True when the agent-passed country (if any) doesn't contradict the
        working location — name match or literal code match."""
        if not country:
            return True
        if slugs_match(country, wl.country):
            return True
        return bool(
            wl.country_code
            and _ALPHA2_RE.match(country)
            and country.lower() == wl.country_code
        )

    async def _geo_entity(
        self,
        country_code: str,
        city: str,
        neighborhood: str | None,
        *,
        confidence: float,
    ) -> ResolvedEntity:
        """Registry-resolved entity for a (city, neighborhood) ask.

        `mint=True`: research meeting a new area is the same first meeting
        a save is, and the mint's round-trip verification *is* this stage's
        verified-geocode. A city that won't verify clarifies; a neighborhood
        that won't verify degrades to the verified city's key — ancestors
        still answer, and no key is invented for an unverified name.
        """
        resolved = await self._registry.key_for_location(
            country_code, city, neighborhood, mint=True
        )
        if resolved is None or resolved.city is None:
            return _clarify(
                f"could not build a verified key for {neighborhood or city}",
                empty="unresolved",
            )
        if neighborhood and resolved.area is not None:
            display = await self._registry.display_row(resolved.area)
            return ResolvedEntity(
                entity_key=display.geo_key,
                entity_type="neighborhood",
                entity_name=display.display_name,
                confidence=confidence,
            )
        return ResolvedEntity(
            entity_key=resolved.city.geo_key,
            entity_type="city",
            entity_name=resolved.city.display_name,
            confidence=confidence,
        )


def _country_entity(
    country_code: str, name: str, *, confidence: float
) -> ResolvedEntity:
    return ResolvedEntity(
        entity_key=country_code.strip().lower(),
        entity_type="country",
        entity_name=name,
        confidence=confidence,
    )
