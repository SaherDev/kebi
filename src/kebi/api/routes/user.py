"""User-scoped routes (/v1/user/...): saved-places library + AI-data erase."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from kebi.api.deps import (
    GatewayIdentity,
    get_event_dispatcher,
    get_place_notes_service,
    get_user_data_deletion_service,
    get_user_intent_service,
    get_user_places_service,
    require_gateway_identity,
)
from kebi.api.rate_limit import limiter
from kebi.api.schemas.intents import IntentsQuery, IntentsResponse
from kebi.api.schemas.library import (
    LibraryQuery,
    LibraryResponse,
    LibraryUserData,
    SaveUserPlaceRequest,
    UserPlaceStatusPatch,
)
from kebi.core.events.dispatcher import EventDispatcher
from kebi.core.events.events import LibraryStateChanged, RecommendationSaved
from kebi.core.knowledge.place_notes_service import PlaceNotesService
from kebi.core.places import (
    PlaceNotFoundError,
    PlaceSource,
    SaveLimitExceededError,
    UserPlacesService,
)
from kebi.core.user.intent_service import UserIntentService
from kebi.core.user.service import DataScope, UserDataDeletionService

router = APIRouter()


@router.get("/user/library", response_model=LibraryResponse)
@limiter.limit("60/minute")
async def get_user_library(
    request: Request,
    identity: Annotated[GatewayIdentity, Depends(require_gateway_identity)],
    params: Annotated[LibraryQuery, Query()],
    service: UserPlacesService = Depends(get_user_places_service),  # noqa: B008
    notes_service: PlaceNotesService = Depends(get_place_notes_service),  # noqa: B008
) -> LibraryResponse:
    """Browse the caller's saved places (the Library screen).

    Returns one filtered page of the user's saved places (`user_places ⋈
    places`) plus an opaque `next_cursor` for the next page (`null` on the
    last page), `total`, the unfiltered grand total of the caller's saves
    (the screen's hero count, the same on every page), and `filtered_total`,
    how many saves match this request's `q`/filters across the whole library.
    An empty library returns an empty list with both counts at 0 — the
    empty-state UI is the product's concern.

    `q` is free-text search over the *whole* library — place name, aliases,
    city, neighbourhood, tags, categories — as a case-insensitive substring,
    so it matches while the user is still typing. It is a predicate, not a
    relevance query: rows are narrowed, never reordered, so `sort` and
    `cursor` are unaffected. Both counts are server-side by necessity —
    filtering client-side over loaded pages makes a saved place three pages
    down report as "no results", which is the failure this exists to remove.

    `sort` is the screen's recent ↔ A–Z toggle: `recent` (newest-saved
    first, default) or `name` (case-insensitive A–Z). A `cursor` is bound to
    the sort it was issued under; replaying it with a different `sort` is a
    400 — switching the toggle restarts paging from the first page.

    Filters (`category`, `tag`, `city`, `country`, `source`, `visited`,
    `liked`, `approved`, `saved_after`, `saved_before`) combine with AND.
    By default every save is returned regardless of its `approved` flag;
    pass `approved=` to split curated vs. needs-review.

    Each place also carries `claims` — the insider notes tied to it from the
    knowledge layer (ADR-127): harvested from shared content or the user's own
    saved-recommendation reasons, only approved claims, strongest first, with a
    `from_shared` flag on notes mined from the very post the user shared. A
    place with no claims returns an empty list.

    `user_id` comes only from the verified gateway identity — a caller can
    only ever read their own library. A malformed or sort-mismatched
    `cursor` surfaces as a 400 via the shared `ValueError` handler.
    """
    places, next_cursor, total, filtered_total = await service.browse(
        identity.user_id,
        params.to_filters(),
        params.limit,
        cursor=params.cursor,
        sort=params.sort,
    )
    notes_by_place = await notes_service.notes_for_saves(places, identity.user_id)
    return LibraryResponse.from_page(
        places, next_cursor, total, filtered_total, notes_by_place
    )


@router.get("/user/intents", response_model=IntentsResponse)
@limiter.limit("60/minute")
async def get_user_intents(
    request: Request,
    identity: Annotated[GatewayIdentity, Depends(require_gateway_identity)],
    params: Annotated[IntentsQuery, Query()],
    service: UserIntentService = Depends(get_user_intent_service),  # noqa: B008
) -> IntentsResponse:
    """The caller's "what you wanted" recall list (the home screen history).

    Returns one newest-first page of the user's past intent-bearing chat turns
    — the natural-language text they typed, verbatim — plus an opaque
    `next_cursor` for the next page (`null` on the last page). `created_at` is
    a raw ISO-8601 instant; the client renders relative phrasing ("yesterday,
    8:42") since only it knows the user's timezone. Tapping a row re-submits
    its `text` to POST /v1/chat. An empty history returns `{"intents": [],
    "next_cursor": null}`.

    `user_id` comes only from the verified gateway identity — a caller can only
    ever read their own intents (ADR-105). A malformed `cursor` surfaces as a
    400 via the shared `ValueError` handler.
    """
    records, next_cursor = await service.list_intents(
        identity.user_id, params.limit, params.cursor
    )
    return IntentsResponse.from_page(records, next_cursor)


@router.post(
    "/user/places",
    response_model=LibraryUserData,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit("60/minute")
async def save_user_place(
    request: Request,
    body: SaveUserPlaceRequest,
    identity: Annotated[GatewayIdentity, Depends(require_gateway_identity)],
    service: UserPlacesService = Depends(get_user_places_service),  # noqa: B008
    event_dispatcher: EventDispatcher = Depends(get_event_dispatcher),  # noqa: B008
) -> LibraryUserData:
    """Save a place kebi surfaced to the caller's library (the place screen's
    "save" action, ADR-151).

    Links the already-cataloged place to the caller as a `user_places` row
    with `source=kebi`, and emits a `RecommendationSaved` event — the strong
    positive taste signal. No turn context is required: the only way a client
    holds a `places.id` is off a `kebi://venue/{id}` link kebi produced, so
    reaching this endpoint at all is what marks the save as kebi-recommended
    (its own `saved_recommendation` interaction type, weighted heavier than a
    passive link-share save, and not counted toward the `source`
    distribution).

    Idempotent: a re-tap on an already-saved place returns the existing save
    and does **not** re-emit the signal, so saving twice never double-trains
    taste. Returns 404 when `place_core_id` is not in the catalog (the save
    trips the `place_id → places.id` foreign key).

    `user_id` comes only from the verified gateway identity, never the body —
    a caller can only ever save into their own library. Returns the created
    (or existing) user-state as `LibraryUserData` (never the raw domain model
    — ADR-105).

    Returns 403 `save_limit_reached` when the caller's plan-tier `save_limit`
    (forwarded by the gateway) is already met — the gateway maps it to the
    upgrade prompt. A re-tap on an already-saved place never counts against
    the cap, so it succeeds even at the limit.
    """
    try:
        user_place, created = await service.save_one(
            identity.user_id,
            body.place_core_id,
            PlaceSource.kebi,
            save_limit=identity.save_limit,
        )
    except PlaceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="place_not_found"
        ) from exc
    except SaveLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="save_limit_reached"
        ) from exc

    if created:
        await event_dispatcher.dispatch(
            RecommendationSaved(
                user_id=identity.user_id,
                place_core_id=body.place_core_id,
            )
        )
    return LibraryUserData.from_user_place(user_place)


@router.patch("/user/places/{user_place_id}", response_model=LibraryUserData)
@limiter.limit("60/minute")
async def update_user_place(
    request: Request,
    user_place_id: str,
    body: UserPlaceStatusPatch,
    identity: Annotated[GatewayIdentity, Depends(require_gateway_identity)],
    service: UserPlacesService = Depends(get_user_places_service),  # noqa: B008
    event_dispatcher: EventDispatcher = Depends(get_event_dispatcher),  # noqa: B008
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

    A change to a taste-relevant pill (visited / liked / approved) emits
    `LibraryStateChanged`, which retrains taste from the new snapshot (ADR-115).
    A note-only edit does not — a note does not affect taste.

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

    if body.model_fields_set & {"visited", "liked", "approved"}:
        await event_dispatcher.dispatch(LibraryStateChanged(user_id=identity.user_id))
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

    Default (no `scope`): hard-deletes every trace — sweeps the five
    user-scoped tables (interactions, user_memories, taste_model,
    user_intents, user_places) in one transaction, deletes the LangGraph
    checkpoint thread, and cancels any pending taste-regen task. The shared
    places/embeddings catalog is left intact (cross-user, not this
    user's data). Idempotent — calling on an absent user is still 204.

    `scope=chat_history`: clears the LangGraph checkpoint thread, pending
    taste-regen, and the "what you wanted" recall list (user_intents),
    which is surfaced conversation history (ADR-110). Saves, memories, and
    the taste model stay intact. Useful for resetting an agent that learned
    a stale pattern (e.g. a URL that used to time out) without losing the
    user's data.

    Unknown scope values are rejected by FastAPI's enum validation
    with a 422 response — no manual error path needed.

    Does NOT delete the user account — NestJS owns user lifecycle. The
    product repo calls this endpoint as part of its account-delete flow
    (no `scope`).
    """
    scopes = set(scope) if scope else None
    await service.delete_user_data(identity.user_id, scopes=scopes)
