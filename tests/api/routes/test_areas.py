"""Tests for GET /v1/areas/{area_id} — the area screen behind every area
link (ADR-153).

The route's contract: the encoded id decodes to a geo key or 404s; an
unprofiled key still answers (thin) and dispatches the background profiler;
a profiled key answers dressed and dispatches nothing; identity comes from
the gateway and the personal fields are the caller's own.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kebi.api.deps import (
    GatewayIdentity,
    get_area_screen_service,
    get_event_dispatcher,
    get_geo_registry,
    require_gateway_identity,
)
from kebi.api.routes.areas import router as areas_router
from kebi.core.areas.keys import encode_area_id
from kebi.core.areas.models import AreaChip, AreaScreen, SectionArea, SectionVenue
from kebi.core.events.events import AreaProfileRequested
from tests.geo_fakes import FakeGeoRegistry, make_area, make_city

_TEST_USER_ID = "user_test_dummy_123456789012345"

# Geo keys are registry id-paths now. Canggu's row records the slug key its
# pre-migration links carried, so old tokens still land on its screen.
_BALI = make_city("id", "Bali")
_CANGGU = make_area(_BALI, "Canggu", legacy_key="id/bali/canggu")


def _dressed_screen() -> AreaScreen:
    return AreaScreen(
        geo_key=_CANGGU.geo_key,
        name="Canggu",
        level="neighbourhood",
        icon="🏄",
        summary="the surf-and-laptop end of bali.",
        best_for=[AreaChip(icon="🌅", text="sunset drinks")],
        saved_count=2,
        profiled=True,
        section_kind="saved",
        venues=[
            SectionVenue(
                place_id="p1",
                name="Savaya Bali",
                icon="🍸",
                subtitle="beach club",
                liked=True,
            )
        ],
    )


def _thin_screen() -> AreaScreen:
    return AreaScreen(geo_key=_BALI.geo_key, name="Bali", profiled=False)


def _make_app(screen: AreaScreen) -> tuple[TestClient, AsyncMock, AsyncMock]:
    screen_service = AsyncMock(build_screen=AsyncMock(return_value=screen))
    dispatcher = AsyncMock(dispatch=AsyncMock())
    app = FastAPI()
    app.include_router(areas_router, prefix="/v1")
    app.dependency_overrides[get_area_screen_service] = lambda: screen_service
    app.dependency_overrides[get_event_dispatcher] = lambda: dispatcher
    app.dependency_overrides[get_geo_registry] = lambda: FakeGeoRegistry(_BALI, _CANGGU)
    app.dependency_overrides[require_gateway_identity] = lambda: GatewayIdentity(
        user_id=_TEST_USER_ID
    )
    client = TestClient(app, raise_server_exceptions=False)
    return client, screen_service, dispatcher


def test_a_profiled_area_answers_dressed_and_dispatches_nothing() -> None:
    client, screen_service, dispatcher = _make_app(_dressed_screen())

    response = client.get(f"/v1/areas/{encode_area_id(_CANGGU.geo_key)}")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Canggu"
    assert body["summary"].startswith("the surf-and-laptop")
    assert body["best_for"][0]["text"] == "sunset drinks"
    assert body["saved_count"] == 2
    assert body["section"]["kind"] == "saved"
    assert body["section"]["places"][0]["uri"] == "kebi://venue/p1"
    # The row's own uri round-trips through the same codec the linkifier uses.
    assert body["uri"] == f"kebi://area/{encode_area_id(_CANGGU.geo_key)}"
    screen_service.build_screen.assert_awaited_once_with(_CANGGU.geo_key, _TEST_USER_ID)
    dispatcher.dispatch.assert_not_awaited()


def test_an_unprofiled_area_answers_thin_and_dispatches_the_profiler() -> None:
    client, _, dispatcher = _make_app(_thin_screen())

    response = client.get(f"/v1/areas/{encode_area_id(_BALI.geo_key)}")

    assert response.status_code == 200
    body = response.json()
    assert body["profiled"] is False
    assert body["summary"] is None
    assert body["section"] is None
    event = dispatcher.dispatch.await_args.args[0]
    assert isinstance(event, AreaProfileRequested)
    assert event.geo_key == _BALI.geo_key
    assert event.user_id == _TEST_USER_ID


def test_a_sub_area_row_links_through_the_codec() -> None:
    screen = _dressed_screen().model_copy(
        update={
            "geo_key": _BALI.geo_key,
            "sub_areas": [
                SectionArea(geo_key=_CANGGU.geo_key, name="Canggu", saved_count=2)
            ],
            "venues": [],
        }
    )
    client, _, _ = _make_app(screen)

    response = client.get(f"/v1/areas/{encode_area_id(_BALI.geo_key)}")

    row = response.json()["section"]["areas"][0]
    assert row["key"] == _CANGGU.geo_key
    assert row["uri"] == f"kebi://area/{encode_area_id(_CANGGU.geo_key)}"


def test_a_garbage_id_is_not_found() -> None:
    client, screen_service, _ = _make_app(_dressed_screen())

    response = client.get("/v1/areas/not-a-real-token!!")

    assert response.status_code == 404
    assert response.json()["detail"] == "area_not_found"
    screen_service.build_screen.assert_not_awaited()


def test_a_raw_geo_key_is_not_an_id() -> None:
    # The old raw-key form must not resolve — links always carry the token.
    client, _, _ = _make_app(_dressed_screen())

    assert client.get("/v1/areas/id").status_code == 404


def test_a_legacy_token_resolves_to_its_recorded_row() -> None:
    """A token minted before the id migration decodes to the old slug key;
    the registry's recorded legacy key translates it, which is what keeps
    every area link in an old chat message alive."""
    client, screen_service, _ = _make_app(_dressed_screen())

    response = client.get(f"/v1/areas/{encode_area_id('id/bali/canggu')}")

    assert response.status_code == 200
    assert response.json()["name"] == "Canggu"
    # The screen is built for the row's CURRENT key, never the slug.
    screen_service.build_screen.assert_awaited_once_with(_CANGGU.geo_key, _TEST_USER_ID)


def test_an_unknown_legacy_token_is_not_found() -> None:
    client, screen_service, _ = _make_app(_dressed_screen())

    response = client.get(f"/v1/areas/{encode_area_id('id/bali/ubud')}")

    assert response.status_code == 404
    assert response.json()["detail"] == "area_not_found"
    screen_service.build_screen.assert_not_awaited()
