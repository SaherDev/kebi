"""Area screen composition — one geo key, one caller, one screen (ADR-153).

The screen is two halves stitched here. The global half comes off the area's
profile row (or arrives thin when no row exists yet — the route dispatches
the profiler and this response shows what it can). The personal half is the
caller's own footprint under the key: their saves, grouped into child-area
rows at wide levels and venue rows at the leaf, exactly the drill-down the
mocks draw — Bali routes you to Canggu instead of repeating its venues.

Saves are scoped by computing each saved place's geo key with the same
`build_geo_key` the claims writers use, so a save and a claim about the same
place always agree on which area contains it. With no saves under the key,
the body falls back to the profile's notable children ("worth knowing");
with neither, the profile and the ask bar carry the screen alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kebi.core.knowledge.schemas import build_geo_key
from kebi.core.places._place_utils import display_place_name
from kebi.core.places.models import PlaceCore, UserPlace
from kebi.core.places.tags import TagType

from .keys import display_from_slug, parent_keys
from .models import (
    AreaProfile,
    AreaScreen,
    BreadcrumbItem,
    SectionArea,
    SectionVenue,
)

if TYPE_CHECKING:
    from kebi.core.places.places_repo import PlacesRepo
    from kebi.core.places.user_places_repo import UserPlacesRepo
    from kebi.db.repositories.area_repository import AreaRepository

# A hook line ("beach clubs · cafés · surf") and a venue subtitle
# ("breakfast · fast wifi") are both capped at this many parts — list rows,
# not paragraphs.
_MAX_HOOK_PARTS = 3


def _pretty(value: str) -> str:
    """`outdoor_seating` → `outdoor seating` — enum values as list-row prose."""
    return value.replace("_", " ")


def _venue_subtitle(core: PlaceCore) -> str | None:
    """Category + top experiential tags, the mock's one-line row subtitle."""
    parts: list[str] = []
    if core.categories:
        parts.append(_pretty(core.categories[0].value))
    for tag in core.tags:
        if len(parts) >= _MAX_HOOK_PARTS:
            break
        tag_type = tag.type.value if isinstance(tag.type, TagType) else tag.type
        if tag_type in ("atmosphere", "feature", "cuisine"):
            value = _pretty(tag.value)
            if value not in parts:
                parts.append(value)
    return " · ".join(parts) or None


def _group_hook(cores: list[PlaceCore]) -> str | None:
    """Fallback hook for an unprofiled child area: what you saved there is
    what it means to you, so its saved places' tags become the line."""
    seen: list[str] = []
    for core in cores:
        for tag in core.tags:
            value = _pretty(tag.value)
            if value not in seen:
                seen.append(value)
            if len(seen) >= _MAX_HOOK_PARTS:
                return " · ".join(seen)
    return " · ".join(seen) or None


def _venue_row(core: PlaceCore, save: UserPlace) -> SectionVenue:
    return SectionVenue(
        place_id=core.id or "",
        name=display_place_name(core.place_name),
        icon=core.icon,
        subtitle=_venue_subtitle(core),
        liked=save.liked,
        visited=save.visited,
    )


class AreaScreenService:
    """Compose the `GET /v1/areas/{id}` screen for one caller (ADR-153)."""

    def __init__(
        self,
        area_repo: AreaRepository,
        user_places_repo: UserPlacesRepo,
        places_repo: PlacesRepo,
    ) -> None:
        self._area_repo = area_repo
        self._user_places_repo = user_places_repo
        self._places_repo = places_repo

    async def build_screen(self, geo_key: str, user_id: str) -> AreaScreen:
        profile = await self._area_repo.get(geo_key)
        in_scope = await self._saves_under(geo_key, user_id)

        parts = geo_key.split("/")
        sub_areas: list[SectionArea] = []
        venues: list[SectionVenue] = []
        if in_scope:
            if len(parts) == 3:
                venues = self._leaf_venues(in_scope)
            else:
                sub_areas, venues = await self._grouped(geo_key, in_scope)
            section_kind = "saved"
        elif profile is not None and profile.notable_sub_areas and len(parts) < 3:
            sub_areas = [
                SectionArea(
                    geo_key=sub.geo_key,
                    name=sub.name,
                    icon=sub.icon,
                    hook=sub.hook,
                )
                for sub in profile.notable_sub_areas
            ]
            section_kind = "worth_knowing"
        else:
            section_kind = None

        return AreaScreen(
            geo_key=geo_key,
            name=profile.name if profile else display_from_slug(parts[-1]),
            level=profile.level if profile else None,
            icon=profile.icon if profile else None,
            summary=profile.summary if profile else None,
            best_for=profile.best_for if profile else [],
            breadcrumb=await self._breadcrumb(geo_key, profile),
            saved_count=len(in_scope),
            profiled=profile is not None,
            section_kind=section_kind,
            sub_areas=sub_areas,
            venues=venues,
        )

    async def _saves_under(
        self, geo_key: str, user_id: str
    ) -> list[tuple[str, PlaceCore, UserPlace]]:
        """The caller's saves inside this area, each with its computed key.

        A save whose place lacks the geo fields a key needs simply cannot be
        placed on any area screen — skipped, not guessed. The full-library
        read is fine: saves are capped per plan and the library pages the
        same order of rows.
        """
        saves = await self._user_places_repo.get_by_user(user_id)
        if not saves:
            return []
        by_place = {s.place_id: s for s in saves}
        cores = await self._places_repo.get_by_ids(list(by_place))
        in_scope: list[tuple[str, PlaceCore, UserPlace]] = []
        for core in cores:
            loc = core.location
            if loc is None or not loc.country_code or not loc.city or not core.id:
                continue
            try:
                key = build_geo_key(loc.country_code, loc.city, loc.neighborhood)
            except ValueError:
                continue
            if key == geo_key or key.startswith(f"{geo_key}/"):
                in_scope.append((key, core, by_place[core.id]))
        return in_scope

    def _leaf_venues(
        self, in_scope: list[tuple[str, PlaceCore, UserPlace]]
    ) -> list[SectionVenue]:
        ordered = sorted(in_scope, key=lambda t: t[2].saved_at, reverse=True)
        return [_venue_row(core, save) for _, core, save in ordered]

    async def _grouped(
        self, geo_key: str, in_scope: list[tuple[str, PlaceCore, UserPlace]]
    ) -> tuple[list[SectionArea], list[SectionVenue]]:
        """Wide-level body: saves grouped into child-area rows.

        A save keyed exactly at this level (its place carries no deeper geo)
        has no child row to live under, so it appears as a venue row after
        the areas — hiding it would make `saved_count` count rows the screen
        never shows.
        """
        depth = len(geo_key.split("/"))
        groups: dict[str, list[tuple[str, PlaceCore, UserPlace]]] = {}
        direct: list[tuple[str, PlaceCore, UserPlace]] = []
        for key, core, save in in_scope:
            child_parts = key.split("/")
            if len(child_parts) > depth:
                child_key = "/".join(child_parts[: depth + 1])
                groups.setdefault(child_key, []).append((key, core, save))
            else:
                direct.append((key, core, save))

        # A profiled child supplies its own name/icon/hook; an unprofiled one
        # is named from its saved places' stored display geo and hooked from
        # their tags.
        profiles = await self._area_repo.get_many(list(groups))
        sub_areas: list[SectionArea] = []
        for child_key, members in sorted(
            groups.items(), key=lambda kv: (-len(kv[1]), kv[0])
        ):
            child_profile = profiles.get(child_key)
            cores = [core for _, core, _ in members]
            name = child_profile.name if child_profile else None
            if name is None:
                loc = cores[0].location
                display = (
                    (loc.city if depth == 1 else loc.neighborhood) if loc else None
                )
                name = display or display_from_slug(child_key.rsplit("/", 1)[-1])
            hook = None
            if child_profile and child_profile.best_for:
                hook = " · ".join(
                    c.text for c in child_profile.best_for[:_MAX_HOOK_PARTS]
                )
            sub_areas.append(
                SectionArea(
                    geo_key=child_key,
                    name=name,
                    icon=child_profile.icon if child_profile else None,
                    hook=hook or _group_hook(cores),
                    saved_count=len(members),
                )
            )
        return sub_areas, self._leaf_venues(direct)

    async def _breadcrumb(
        self, geo_key: str, profile: AreaProfile | None
    ) -> list[BreadcrumbItem]:
        """Tappable ancestors, outermost first.

        Names prefer the ancestor's own row (it may already be profiled),
        then the names this profile's dressing recorded, then the slug —
        so `id/bali/canggu` reads `Indonesia › Bali` once anything has
        named those levels, and degrades to readable slugs before that.
        """
        parents = parent_keys(geo_key)
        if not parents:
            return []
        parent_profiles = await self._area_repo.get_many(parents)
        recorded: list[str] = list(profile.breadcrumb) if profile else []
        items: list[BreadcrumbItem] = []
        for i, parent_key in enumerate(parents):
            parent_profile = parent_profiles.get(parent_key)
            if parent_profile is not None:
                name = parent_profile.name
            elif i < len(recorded) and recorded[i].strip():
                name = recorded[i]
            else:
                name = display_from_slug(parent_key.rsplit("/", 1)[-1])
            items.append(BreadcrumbItem(geo_key=parent_key, name=name))
        return items
