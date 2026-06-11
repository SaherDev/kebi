"""User-scoped routes (/v1/user/...): saved-places library + AI-data erase."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from kebi.api.deps import (
    GatewayIdentity,
    get_user_data_deletion_service,
    get_user_places_service,
    require_gateway_identity,
)
from kebi.api.rate_limit import limiter
from kebi.api.schemas.library import (
    LibraryQuery,
    LibraryResponse,
    LibraryUserData,
    UserPlaceStatusPatch,
)
from kebi.core.places import UserPlacesService
from kebi.core.user.service import DataScope, UserDataDeletionService

router = APIRouter()


@router.get("/user/library", response_model=LibraryResponse)
@limiter.limit("60/minute")
async def get_user_library(
    request: Request,
    identity: Annotated[GatewayIdentity, Depends(require_gateway_identity)],
    params: Annotated[LibraryQuery, Query()],
    service: UserPlacesService = Depends(get_user_places_service),  # noqa: B008
) -> LibraryResponse:
    """Browse the caller's saved places (the Library screen).

    Returns one filtered page of the user's saved places (`user_places ⋈
    places`) plus an opaque `next_cursor` for the next page (`null` on the
    last page) and `total`, the unfiltered grand total of the caller's saves
    (the screen's hero count, the same on every page). An empty library
    returns `{"places": [], "next_cursor": null, "total": 0}` — the
    empty-state UI is the product's concern.

    `sort` is the screen's recent ↔ A–Z toggle: `recent` (newest-saved
    first, default) or `name` (case-insensitive A–Z). A `cursor` is bound to
    the sort it was issued under; replaying it with a different `sort` is a
    400 — switching the toggle restarts paging from the first page.

    Filters (`category`, `tag`, `city`, `country`, `source`, `visited`,
    `liked`, `approved`, `saved_after`, `saved_before`) combine with AND.
    By default every save is returned regardless of its `approved` flag;
    pass `approved=` to split curated vs. needs-review.

    `user_id` comes only from the verified gateway identity — a caller can
    only ever read their own library. A malformed or sort-mismatched
    `cursor` surfaces as a 400 via the shared `ValueError` handler.
    """
    places, next_cursor, total = await service.browse(
        identity.user_id,
        params.to_filters(),
        params.limit,
        cursor=params.cursor,
        sort=params.sort,
    )
    return LibraryResponse.from_page(places, next_cursor, total)


@router.patch("/user/places/{user_place_id}", response_model=LibraryUserData)
@limiter.limit("60/minute")
async def update_user_place(
    request: Request,
    user_place_id: str,
    body: UserPlaceStatusPatch,
    identity: Annotated[GatewayIdentity, Depends(require_gateway_identity)],
    service: UserPlacesService = Depends(get_user_places_service),  # noqa: B008
) -> LibraryUserData:
    """Update one saved place's user-state — the Library pills and menu
    actions (been there / liked / approved / note).

    Partial update: the body carries only the fields that changed. An omitted
    field is left untouched; an explicit `null` clears it (un-like back to
    neutral, erase a note). An empty body is rejected with 422 — a no-op
    patch is a client mistake.

    Ownership is enforced in the update itself — the row is matched on
    `(user_place_id, user_id)`, where `user_id` comes only from the verified
    gateway identity. A caller can only ever mutate their own save.

    Returns the updated user-state (`LibraryUserData`, never the raw domain
    model — ADR-105). When nothing matched — the save does not exist *or*
    belongs to another user — returns 404; the two cases are deliberately
    indistinguishable, so a 404 never reveals another user's save.
    """
    updated = await service.update_status(
        user_place_id, identity.user_id, body.to_update()
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="saved_place_not_found"
        )
    return LibraryUserData.from_user_place(updated)


@router.delete("/user/places/{user_place_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("60/minute")
async def delete_user_place(
    request: Request,
    user_place_id: str,
    identity: Annotated[GatewayIdentity, Depends(require_gateway_identity)],
    service: UserPlacesService = Depends(get_user_places_service),  # noqa: B008
) -> None:
    """Remove one saved place from the caller's library (the Library
    swipe-to-delete / remove action).

    Hard-deletes the single `user_places` row. Ownership is enforced in the
    delete itself — the row is matched on `(user_place_id, user_id)`, where
    `user_id` comes only from the verified gateway identity. A caller can
    only ever delete their own save; a valid `user_place_id` belonging to
    someone else matches nothing.

    Returns 204 when the caller's save was removed. When nothing matched —
    the save does not exist *or* belongs to another user — returns 404. The
    two cases are deliberately indistinguishable, so a 404 never reveals
    whether another user holds that save.

    Deletes only the user's save. The shared catalog place/embeddings are
    left intact (cross-user data, not the caller's), and the taste model is
    not recomputed here.
    """
    deleted = await service.delete_place(user_place_id, identity.user_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="saved_place_not_found"
        )


@router.delete("/user/data", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("3/hour")
async def delete_user_data(
    request: Request,
    identity: Annotated[GatewayIdentity, Depends(require_gateway_identity)],
    scope: Annotated[
        list[DataScope] | None,
        Query(
            description=(
                "Optional list of data scopes to delete. Repeat the param "
                "for multi-value (`?scope=chat_history&scope=...`). "
                "Omit to wipe everything — preserves the NestJS "
                "account-delete contract."
            ),
        ),
    ] = None,
    service: UserDataDeletionService = Depends(get_user_data_deletion_service),  # noqa: B008
) -> None:
    """Selectively delete a user's AI-owned data.

    Default (no `scope`): hard-deletes every trace — sweeps the four
    user-scoped tables (interactions, user_memories, taste_model,
    user_places) in one transaction, deletes the LangGraph checkpoint
    thread, and cancels any pending taste-regen task. The shared
    places/embeddings catalog is left intact (cross-user, not this
    user's data). Idempotent — calling on an absent user is still 204.

    `scope=chat_history`: clears only the LangGraph checkpoint thread
    + pending taste-regen. Saves stay intact. Useful for resetting an
    agent that learned a stale pattern (e.g. a URL that used to time
    out) without losing the user's data.

    Unknown scope values are rejected by FastAPI's enum validation
    with a 422 response — no manual error path needed.

    Does NOT delete the user account — NestJS owns user lifecycle. The
    product repo calls this endpoint as part of its account-delete flow
    (no `scope`).
    """
    scopes = set(scope) if scope else None
    await service.delete_user_data(identity.user_id, scopes=scopes)
