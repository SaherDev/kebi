"""Curation service — structures expert prose and persists it (ADR-121/122).

A thin orchestration over the curator (prose → resolved claims) and the
source-agnostic `KnowledgeIngestion`, which stamps `curated_expert`
provenance from the curator itself. This layer also owns anchor resolution:
turning the request's entity reference (a catalog place id or a decoded area
geo key) into the `CurationAnchor` the curator reasons with — the LLM only
ever sees an already-verified entity, never a raw id to trust. Kept separate
from the route so construction stays in the wiring layer and the route stays
a thin adapter.
"""

from __future__ import annotations

from kebi.core.areas.keys import display_from_slug
from kebi.core.knowledge.curator import KnowledgeCurator
from kebi.core.knowledge.producer import KnowledgeIngestion
from kebi.core.knowledge.schemas import (
    CurationAnchor,
    KnowledgeClaim,
    ResolvedGeo,
    WrittenClaim,
)
from kebi.core.places.protocols import PlacesRepoProtocol
from kebi.db.repositories.area_repository import AreaRepository
from kebi.db.repositories.knowledge_claim_repository import KnowledgeClaimRepository


class AnchorNotFoundError(LookupError):
    """The request's anchor names a place the catalog doesn't have."""


def curator_source_ref(user_id: str) -> str:
    """The provenance ref a curated claim carries — also its ownership key.

    Curated claims are global (`user_id` NULL), so this ref is the only
    record of who wrote one; list-mine and retract both match on it.
    """
    return f"curator:{user_id}"


class KnowledgeCurationService:
    def __init__(
        self,
        curator: KnowledgeCurator,
        ingestion: KnowledgeIngestion,
        places_repo: PlacesRepoProtocol,
        area_repo: AreaRepository,
    ) -> None:
        self._curator = curator
        self._ingestion = ingestion
        self._places_repo = places_repo
        self._area_repo = area_repo

    async def curate(
        self,
        *,
        text: str,
        user_id: str,
        anchor_place_id: str | None = None,
        anchor_geo_key: str | None = None,
    ) -> list[WrittenClaim]:
        """Structure `text` into claims and persist them as `curated_expert`
        (global, `user_id=None`). At most one anchor reference may be set
        (the route enforces that shape); it is resolved to a `CurationAnchor`
        before the LLM runs — an unknown place raises `AnchorNotFoundError`.
        Returns the claims that produced a new row, each with its id (dedup
        collapses re-submissions). The caller's `user_id` is recorded only as
        provenance (`source_ref`), never as a claim scope."""
        anchor = await self._resolve_anchor(anchor_place_id, anchor_geo_key)
        structured = await self._curator.structure(text, anchor, user_id=user_id)
        return await self._ingestion.ingest(
            self._curator,
            structured,
            source_ref=curator_source_ref(user_id),
            user_id=None,
        )

    async def _resolve_anchor(
        self, place_id: str | None, geo_key: str | None
    ) -> CurationAnchor | None:
        if place_id is not None:
            return await self._resolve_place_anchor(place_id)
        if geo_key is not None:
            return await self._resolve_area_anchor(geo_key)
        return None

    async def _resolve_place_anchor(self, place_id: str) -> CurationAnchor:
        found = await self._places_repo.get_by_ids([place_id])
        if not found:
            raise AnchorNotFoundError(place_id)
        place = found[0]
        loc = place.location
        geo = ResolvedGeo(
            country_code=loc.country_code if loc else None,
            city=loc.city if loc else None,
            neighborhood=loc.neighborhood if loc else None,
        )
        return CurationAnchor(place_id=place_id, name=place.place_name, geo=geo)

    async def _resolve_area_anchor(self, geo_key: str) -> CurationAnchor:
        """Any decodable geo key is a valid anchor — area rows are lazy
        (ADR-153), so absence only means the profiler hasn't dressed it yet
        and the display name falls back to the key's own slug."""
        parts = geo_key.strip("/").split("/")
        profile = await self._area_repo.get(geo_key)
        name = profile.name if profile else display_from_slug(parts[-1])
        geo = ResolvedGeo(
            country_code=parts[0],
            city=parts[1] if len(parts) > 1 else None,
            neighborhood=parts[2] if len(parts) > 2 else None,
        )
        return CurationAnchor(place_id=None, name=name, geo=geo)


class CuratorClaimsService:
    """Read/retract path over a curator's own claims.

    Ownership is the source_ref, never the row's user_id — curated claims
    are global on purpose, and this is the one seam that reconstructs "mine"
    from provenance. Both operations take the caller's identity, so a caller
    can never list or delete past their own ref.
    """

    def __init__(self, repo: KnowledgeClaimRepository) -> None:
        self._repo = repo

    async def list_mine(
        self, user_id: str, limit: int, cursor: str | None
    ) -> tuple[list[KnowledgeClaim], str | None]:
        return await self._repo.list_by_source_ref(
            curator_source_ref(user_id), limit, cursor
        )

    async def retract(self, user_id: str, claim_id: str) -> bool:
        """True when the claim existed, was the caller's, and is now gone."""
        return await self._repo.delete_owned(claim_id, curator_source_ref(user_id))
