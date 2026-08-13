"""GET /v1/places/{place_id} — the place screen behind every venue link.

Chat renders text plus `kebi://venue/{id}` links (ADR-136), and until now a
link only resolved to data the client already held: a saved place lived in
the library, an unsaved one evaporated when the turn scrolled away. This
route makes every venue link openable (ADR-151) — the id is `places.id`, the
catalog row every surfaced place already has, so a suggested place is as
tappable as a saved one.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from kebi.api.deps import (
    GatewayIdentity,
    get_event_dispatcher,
    get_place_notes_service,
    get_places_repo,
    get_user_places_service,
    require_gateway_identity,
)
from kebi.api.rate_limit import limiter
from kebi.api.schemas.library import LibraryItem
from kebi.core.events.dispatcher import EventDispatcher
from kebi.core.events.events import PlaceProfileRequested
from kebi.core.knowledge.place_notes_service import PlaceNotesService
from kebi.core.places import UserPlacesService
from kebi.core.places.profile_service import needs_profile
from kebi.core.places.protocols import PlacesRepoProtocol

router = APIRouter()


@router.get("/places/{place_id}", response_model=LibraryItem)
@limiter.limit("60/minute")
async def get_place(
    request: Request,
    place_id: str,
    identity: Annotated[GatewayIdentity, Depends(require_gateway_identity)],
    places_repo: PlacesRepoProtocol = Depends(get_places_repo),  # noqa: B008
    user_places: UserPlacesService = Depends(get_user_places_service),  # noqa: B008
    notes_service: PlaceNotesService = Depends(get_place_notes_service),  # noqa: B008
    event_dispatcher: EventDispatcher = Depends(get_event_dispatcher),  # noqa: B008
) -> LibraryItem:
    """One catalog place as the client renders it, saved or not.

    Returns the same `LibraryItem` shape a Library page uses — catalog
    `place`, `user_data`, `claims` — so a venue tap and a library row open
    the identical screen. The one difference: `user_data` is null when the
    caller never saved this place, which is exactly what tells the screen to
    offer "save" (`POST /v1/user/places` with this same id) instead of the
    pills.

    `claims` are the place's insider notes (ADR-127): global approved claims
    plus the caller's own, never another user's. `from_shared` can only be
    true when the caller holds a save with a matching share ref — an unsaved
    place's notes are all simply global.

    404 when `place_id` is not in the catalog — venue links kebi minted
    always resolve, so this means a stale or fabricated id. `user_id` comes
    only from the verified gateway identity: the place is public catalog
    data, but whose save/notes ride along is never the caller's choice.
    """
    cores = await places_repo.get_by_ids([place_id])
    if not cores:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="place_not_found"
        )
    if needs_profile(cores[0]):
        # A row with no experiential tags entered via the provider
        # write-through and no LLM ever looked at it — profile it in the
        # background so the screen has something to show next open
        # (ADR-152). This response still returns the thin row.
        await event_dispatcher.dispatch(
            PlaceProfileRequested(user_id=identity.user_id, place_id=place_id)
        )
    save = await user_places.get_save(identity.user_id, place_id)
    notes = await notes_service.notes_for_place(
        place_id,
        identity.user_id,
        save_ref=save.source_ref if save else None,
    )
    return LibraryItem.from_place(cores[0], save, notes)
