"""User-scoped routes (/v1/user/...): saved-places library + AI-data erase."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from kebi.api.deps import (
    GatewayIdentity,
    get_user_data_deletion_service,
    get_user_places_service,
    require_gateway_identity,
)
from kebi.api.rate_limit import limiter
from kebi.api.schemas.library import LibraryQuery, LibraryResponse
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

    Returns one filtered, newest-first page of the user's saved places
    (`user_places ⋈ places`) plus an opaque `next_cursor` for the next page
    (`null` on the last page). An empty library returns `{"places": [],
    "next_cursor": null}` — the empty-state UI is the product's concern.

    Filters (`category`, `tag`, `city`, `country`, `source`, `visited`,
    `liked`, `approved`, `saved_after`, `saved_before`) combine with AND.
    By default every save is returned regardless of its `approved` flag;
    pass `approved=` to split curated vs. needs-review.

    `user_id` comes only from the verified gateway identity — a caller can
    only ever read their own library. A malformed `cursor` surfaces as a
    400 via the shared `ValueError` handler.
    """
    places, next_cursor = await service.browse(
        identity.user_id,
        params.to_filters(),
        params.limit,
        cursor=params.cursor,
    )
    return LibraryResponse(places=places, next_cursor=next_cursor)


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
