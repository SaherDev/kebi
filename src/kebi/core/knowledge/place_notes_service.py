"""Place-notes read service — the knowledge layer's first reader (ADR-127).

Turns the claims tied to a place into the insider notes shown on a saved place
in the Library. This is the first thing that *reads* `knowledge_claims`
(everything before it only wrote). v1 is place-scoped: it pulls claims keyed to
the exact place (`place:<id>`) — the reliable, indexed link — not by the share
URL. It still tells the client which notes came from the user's own shared post
by comparing each claim's `source_ref` to the save's, exposed as `from_shared`.

Only approved claims surface (`approved_only=True`, ADR-122). A place with no
claims returns an empty list, so the section degrades gracefully.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import TYPE_CHECKING

from kebi.core.knowledge.schemas import KnowledgeClaim, PlaceNote, build_place_key
from kebi.db.repositories.knowledge_claim_repository import KnowledgeClaimRepository

if TYPE_CHECKING:
    from kebi.core.places.models import SavedPlaceView


class PlaceNotesService:
    def __init__(self, repo: KnowledgeClaimRepository, *, limit: int) -> None:
        self._repo = repo
        self._limit = limit

    async def notes_for_saves(
        self, saves: Sequence[SavedPlaceView], user_id: str
    ) -> dict[str, list[PlaceNote]]:
        """Insider notes for each save, keyed by `place.id`.

        One batched read over every place on the page (`place:<id>` keys), then
        grouped and ranked per place. `user_id` scopes the read so a caller
        sees global claims plus their own `kebi_message` reasons, never another
        user's. Saves without a catalog id are skipped."""
        by_key: dict[str, tuple[str, SavedPlaceView]] = {}
        for save in saves:
            place_id = save.place.id
            if place_id is not None:
                by_key[build_place_key(place_id)] = (place_id, save)
        if not by_key:
            return {}

        claims = await self._repo.list_for_entities(
            list(by_key), user_id=user_id, approved_only=True
        )
        grouped: dict[str, list[KnowledgeClaim]] = defaultdict(list)
        for claim in claims:
            grouped[claim.entity_key].append(claim)

        return {
            place_id: self._to_notes(
                grouped.get(entity_key, []), save.user_data.source_ref
            )
            for entity_key, (place_id, save) in by_key.items()
        }

    async def notes_for_place(
        self, place_id: str, user_id: str, save_ref: str | None = None
    ) -> list[PlaceNote]:
        """Insider notes for one place — saved or not (the place screen,
        ADR-151).

        `save_ref` is the caller's save's `source_ref` when they hold one, so
        `from_shared` still marks notes mined from their own shared post; an
        unsaved place has no share to match, so every note is simply global.
        """
        claims = await self._repo.list_for_entities(
            [build_place_key(place_id)], user_id=user_id, approved_only=True
        )
        return self._to_notes(claims, save_ref)

    def _to_notes(
        self, claims: list[KnowledgeClaim], save_ref: str | None
    ) -> list[PlaceNote]:
        """Rank one place's claims and project them to notes, flagging the
        ones that came from the very post the user shared."""
        return [
            PlaceNote(
                id=claim.id,
                text=claim.claim,
                tags=claim.tags,
                source_type=claim.source_type,
                from_shared=(
                    claim.source_ref is not None
                    and save_ref is not None
                    and claim.source_ref == save_ref
                ),
                agree_count=claim.agree_count,
                disagree_count=claim.disagree_count,
            )
            for claim in self._rank(claims)
        ]

    def _rank(self, claims: list[KnowledgeClaim]) -> list[KnowledgeClaim]:
        """Strongest first (confidence, then most recent), capped at the limit.
        A flat order — the client, not the server, decides how to group or
        badge `from_shared`."""
        ordered = sorted(
            claims, key=lambda c: (c.confidence, c.created_at), reverse=True
        )
        return ordered[: self._limit]
