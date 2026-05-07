"""User-scoped routes (/v1/user/...): taste context fetch and AI-data erase."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from kebi.api.deps import (
    get_places_service,
    get_taste_service,
    get_user_data_deletion_service,
)
from kebi.core.places.service import PlacesService
from kebi.core.taste.schemas import UserContext
from kebi.core.taste.service import TasteModelService
from kebi.core.user.service import DataScope, UserDataDeletionService

router = APIRouter()


@router.get("/user/context", response_model=UserContext)
async def get_user_context(
    user_id: str = Query(..., description="User identifier"),  # noqa: B008
    taste_service: TasteModelService = Depends(get_taste_service),  # noqa: B008
    places_service: PlacesService = Depends(get_places_service),  # noqa: B008
) -> UserContext:
    """Compose `saved_places_count` (from the `places` table via
    PlacesService) with the taste-model-derived tier + chips. The count
    is owned outside TasteModelService so cold users (no taste_model row
    yet) still see their real save total.
    """
    saved_places_count = await places_service.count_for_user(user_id)
    taste_context = await taste_service.get_taste_context(user_id)
    return UserContext(
        saved_places_count=saved_places_count,
        signal_tier=taste_context.signal_tier,
        chips=taste_context.chips,
    )


@router.delete("/user/{user_id}/data", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_data(
    user_id: str,
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

    Default (no `scope`): hard-deletes every trace — sweeps the 6 AI
    tables (embeddings cascade from places), deletes the LangGraph
    checkpoint thread, and cancels any pending taste-regen task.
    Idempotent — calling on an absent user is still 204.

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
    await service.delete_user_data(user_id, scopes=scopes)
