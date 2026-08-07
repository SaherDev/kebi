"""Tests for GET /v1/places/{place_id} — the place screen behind every venue
link (ADR-151).

The route's contract: any catalog place resolves, saved or not — the same
`LibraryItem` shape a Library page uses, with `user_data` null exactly when
the caller holds no save (that null is what tells the screen to offer
"save"). Insider notes ride along either way; `from_shared` can only fire
off the caller's own save's share ref. Identity from the gateway, 404 for an
id the catalog does not know.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kebi.api.deps import (
    GatewayIdentity,
    get_event_dispatcher,
    get_place_notes_service,
    get_places_repo,
    get_user_places_service,
    require_gateway_identity,
)
from kebi.api.routes.places import router as places_router
from kebi.core.events.events import PlaceProfileRequested
from kebi.core.knowledge.schemas import PlaceNote
from kebi.core.places import (
    PlaceCore,
    PlaceSource,
    PlaceTag,
    UserPlace,
    UserPlacesService,
)

_TEST_USER_ID = "user_test_dummy_123456789012345"


def _core() -> PlaceCore:
    return PlaceCore(id="p1", place_name="Kala Kala Beach Club")


def _save(**overrides: object) -> UserPlace:
    base = {
        "user_place_id": "up-1",
        "user_id": _TEST_USER_ID,
        "place_id": "p1",
        "approved": True,
        "source": PlaceSource.tiktok,
        "source_ref": "https://tiktok.com/@x/video/1",
        "saved_at": datetime(2026, 6, 9, tzinfo=UTC),
    }
    base.update(overrides)
    return UserPlace(**base)  # type: ignore[arg-type]


def _note() -> PlaceNote:
    return PlaceNote(
        id="c1",
        text="sunset is the slot, daybeds book out on weekends",
        tags=["timing"],
        source_type="shared_content",
        from_shared=True,
        agree_count=0,
        disagree_count=0,
    )


def _make_app(
    cores: list[PlaceCore],
    save: UserPlace | None,
    notes: list[PlaceNote] | None = None,
) -> tuple[TestClient, AsyncMock, AsyncMock, AsyncMock]:
    places_repo = AsyncMock(get_by_ids=AsyncMock(return_value=cores))
    user_places = AsyncMock(spec=UserPlacesService)
    user_places.get_save = AsyncMock(return_value=save)
    notes_service = AsyncMock(notes_for_place=AsyncMock(return_value=notes or []))
    dispatcher = AsyncMock(dispatch=AsyncMock())
    app = FastAPI()
    app.include_router(places_router, prefix="/v1")
    app.dependency_overrides[get_places_repo] = lambda: places_repo
    app.dependency_overrides[get_user_places_service] = lambda: user_places
    app.dependency_overrides[get_place_notes_service] = lambda: notes_service
    app.dependency_overrides[get_event_dispatcher] = lambda: dispatcher
    app.dependency_overrides[require_gateway_identity] = lambda: GatewayIdentity(
        user_id=_TEST_USER_ID
    )
    client = TestClient(app, raise_server_exceptions=False)
    return client, user_places, notes_service, dispatcher


def test_unsaved_place_resolves_with_null_user_data() -> None:
    """The whole point of the route: a suggested place the user never saved
    still opens — `user_data: null` is the screen's "offer save" signal."""
    client, _, _, _ = _make_app([_core()], save=None)

    response = client.get("/v1/places/p1")

    assert response.status_code == 200
    body = response.json()
    assert body["place"]["place_name"] == "Kala Kala Beach Club"
    assert body["user_data"] is None
    assert body["claims"] == []


def test_saved_place_carries_user_data_without_user_id() -> None:
    client, _, _, _ = _make_app([_core()], save=_save())

    response = client.get("/v1/places/p1")

    assert response.status_code == 200
    body = response.json()
    assert body["user_data"]["user_place_id"] == "up-1"
    assert body["user_data"]["source"] == "tiktok"
    # ADR-105 — the caller's identity is never echoed back.
    assert "user_id" not in body["user_data"]


def test_notes_ride_along_with_coarse_source_label() -> None:
    client, _, _, _ = _make_app([_core()], save=_save(), notes=[_note()])

    response = client.get("/v1/places/p1")

    claims = response.json()["claims"]
    assert len(claims) == 1
    assert claims[0]["text"].startswith("sunset is the slot")
    assert claims[0]["from_shared"] is True
    # The wire carries the coarse label, never the raw source_type.
    assert claims[0]["source"] == "community"


def test_save_ref_is_threaded_only_when_a_save_exists() -> None:
    """`from_shared` matching needs the save's share ref — an unsaved place
    reads notes with no ref, so nothing can false-positive as "yours"."""
    client, _, notes_service, _ = _make_app([_core()], save=_save())
    client.get("/v1/places/p1")
    kwargs = notes_service.notes_for_place.await_args.kwargs
    assert kwargs["save_ref"] == "https://tiktok.com/@x/video/1"

    client, _, notes_service, _ = _make_app([_core()], save=None)
    client.get("/v1/places/p1")
    assert notes_service.notes_for_place.await_args.kwargs["save_ref"] is None


def test_unknown_place_returns_404() -> None:
    client, user_places, _, _ = _make_app([], save=None)

    response = client.get("/v1/places/ghost")

    assert response.status_code == 404
    assert response.json()["detail"] == "place_not_found"
    user_places.get_save.assert_not_awaited()


def test_identity_scopes_the_save_lookup() -> None:
    client, user_places, _, _ = _make_app([_core()], save=None)

    client.get("/v1/places/p1")

    user_places.get_save.assert_awaited_once_with(_TEST_USER_ID, "p1")


def test_a_thin_place_triggers_a_background_profile() -> None:
    """A row with no experiential tags dispatches PlaceProfileRequested
    (ADR-152) — the screen shows the thin row now, the tags arrive for the
    next open."""
    client, _, _, dispatcher = _make_app([_core()], save=None)

    client.get("/v1/places/p1")

    dispatcher.dispatch.assert_awaited_once()
    event = dispatcher.dispatch.await_args.args[0]
    assert isinstance(event, PlaceProfileRequested)
    assert event.place_id == "p1"


def test_a_profiled_place_triggers_nothing() -> None:
    core = _core().model_copy(
        update={"tags": [PlaceTag(type="atmosphere", value="lively", source="llm")]}
    )
    client, _, _, dispatcher = _make_app([core], save=None)

    client.get("/v1/places/p1")

    dispatcher.dispatch.assert_not_awaited()


def test_an_unknown_place_triggers_no_profile() -> None:
    client, _, _, dispatcher = _make_app([], save=None)

    client.get("/v1/places/ghost")

    dispatcher.dispatch.assert_not_awaited()
