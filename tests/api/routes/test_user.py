"""Tests for GET /v1/user/context (feature 023)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kebi.api.deps import get_places_service, get_taste_service
from kebi.api.routes.user import router as user_router
from kebi.core.places.service import PlacesService
from kebi.core.taste.schemas import (
    Chip,
    ChipStatus,
    TasteContext,
    TasteProfile,
)
from kebi.core.taste.service import TasteModelService


def _make_app(
    taste_service: TasteModelService,
    places_service: PlacesService,
) -> TestClient:
    app = FastAPI()
    app.include_router(user_router, prefix="/v1")
    app.dependency_overrides[get_taste_service] = lambda: taste_service
    app.dependency_overrides[get_places_service] = lambda: places_service
    return TestClient(app)


def _stub_places_service(saved_count: int) -> AsyncMock:
    svc = AsyncMock(spec=PlacesService)
    svc.count_for_user = AsyncMock(return_value=saved_count)
    return svc


def _stub_service(
    *,
    profile: TasteProfile | None = None,
    user_id: str = "user_abc",
) -> AsyncMock:
    """Build a TasteModelService stub that runs the real get_taste_context."""
    from kebi.core.taste.service import TasteModelService as _Real

    svc = AsyncMock(spec=_Real)
    svc.get_taste_profile = AsyncMock(return_value=profile)

    async def _get_taste_context(uid: str) -> TasteContext:
        from kebi.core.config import get_config
        from kebi.core.taste.schemas import ChipView
        from kebi.core.taste.tier import derive_signal_tier, selection_round_name

        config = get_config()
        stages = config.taste_model.chip_selection_stages
        chip_threshold = config.taste_model.chip_threshold
        p = await svc.get_taste_profile(uid)
        if p is None:
            return TasteContext(
                signal_tier=derive_signal_tier(0, [], stages, chip_threshold),
                chips=[],
            )
        signal_tier = derive_signal_tier(
            signal_count=p.generated_from_log_count,
            chips=p.chips,
            stages=stages,
            chip_threshold=chip_threshold,
        )
        current_sr = selection_round_name(p.generated_from_log_count, stages)
        chips = [
            ChipView(
                label=c.label,
                source_field=c.source_field,
                source_value=c.source_value,
                signal_count=c.signal_count,
                status=c.status,
                selection_round=c.selection_round or current_sr,
            )
            for c in p.chips
        ]
        return TasteContext(
            signal_tier=signal_tier,
            chips=chips,
        )

    svc.get_taste_context = _get_taste_context
    return svc


def test_cold_user_returns_cold_tier_with_real_place_count() -> None:
    svc = _stub_service(profile=None)
    places = _stub_places_service(saved_count=2)
    client = _make_app(svc, places)

    response = client.get("/v1/user/context", params={"user_id": "new_user"})

    assert response.status_code == 200
    body = response.json()
    # Cold tier (no taste_model row) but the 2 places in the DB surface
    # through — the old bug returned 0 because it read signal_counts.
    assert body == {
        "saved_places_count": 2,
        "signal_tier": "cold",
        "chips": [],
    }


def test_warming_user_returns_warming_tier() -> None:
    profile = TasteProfile(
        taste_profile_summary=[],
        signal_counts={"totals": {"saves": 3}},
        chips=[],
        generated_from_log_count=3,
    )
    svc = _stub_service(profile=profile)
    places = _stub_places_service(saved_count=3)
    client = _make_app(svc, places)

    response = client.get("/v1/user/context", params={"user_id": "warm_user"})

    assert response.status_code == 200
    body = response.json()
    assert body["signal_tier"] == "warming"
    assert body["saved_places_count"] == 3
    assert body["chips"] == []


def test_chip_selection_tier_exposes_pending_chip_shape() -> None:
    chip = Chip(
        label="Ramen lover",
        source_field="attributes.cuisine",
        source_value="ramen",
        signal_count=3,
        status=ChipStatus.PENDING,
        selection_round=None,
    )
    profile = TasteProfile(
        taste_profile_summary=[],
        signal_counts={"totals": {"saves": 5}},
        chips=[chip],
        generated_from_log_count=5,
    )
    svc = _stub_service(profile=profile)
    places = _stub_places_service(saved_count=5)
    client = _make_app(svc, places)

    response = client.get("/v1/user/context", params={"user_id": "chip_user"})

    assert response.status_code == 200
    body = response.json()
    assert body["signal_tier"] == "chip_selection"
    assert len(body["chips"]) == 1
    returned = body["chips"][0]
    assert returned["label"] == "Ramen lover"
    assert returned["status"] == "pending"
    # Pending chips get their selection_round stamped with the current
    # crossed-stage name (here "round_1" for signal_count=5). The frontend
    # can echo this back verbatim in a chip_confirm submission.
    assert returned["selection_round"] == "round_1"


def test_active_tier_preserves_confirmed_and_rejected_statuses() -> None:
    chips = [
        Chip(
            label="Ramen lover",
            source_field="attributes.cuisine",
            source_value="ramen",
            signal_count=5,
            status=ChipStatus.CONFIRMED,
            selection_round="round_1",
        ),
        Chip(
            label="Casual spots",
            source_field="attributes.vibe",
            source_value="casual",
            signal_count=2,
            status=ChipStatus.REJECTED,
            selection_round="round_1",
        ),
    ]
    profile = TasteProfile(
        taste_profile_summary=[],
        signal_counts={"totals": {"saves": 12}},
        chips=chips,
        generated_from_log_count=12,
    )
    svc = _stub_service(profile=profile)
    places = _stub_places_service(saved_count=12)
    client = _make_app(svc, places)

    response = client.get("/v1/user/context", params={"user_id": "active_user"})

    assert response.status_code == 200
    body = response.json()
    assert body["signal_tier"] == "active"
    statuses = {c["label"]: (c["status"], c["selection_round"]) for c in body["chips"]}
    # Confirmed/rejected chips keep their original selection_round, not
    # overwritten by the current stage.
    assert statuses["Ramen lover"] == ("confirmed", "round_1")
    assert statuses["Casual spots"] == ("rejected", "round_1")


def test_chip_selection_at_round_2_stamps_round_2_on_pending_chips() -> None:
    # New pending chip that surfaces after the user crossed round_2.
    chip = Chip(
        label="Pho lover",
        source_field="attributes.cuisine",
        source_value="pho",
        signal_count=3,
        status=ChipStatus.PENDING,
        selection_round=None,
    )
    profile = TasteProfile(
        taste_profile_summary=[],
        signal_counts={"totals": {"saves": 20}},
        chips=[chip],
        generated_from_log_count=20,  # round_2 threshold
    )
    svc = _stub_service(profile=profile)
    places = _stub_places_service(saved_count=20)
    client = _make_app(svc, places)

    response = client.get("/v1/user/context", params={"user_id": "r2_user"})

    body = response.json()
    assert body["signal_tier"] == "chip_selection"
    assert body["chips"][0]["selection_round"] == "round_2"


def test_missing_user_id_returns_422() -> None:
    svc = _stub_service(profile=None)
    places = _stub_places_service(saved_count=0)
    client = _make_app(svc, places)

    response = client.get("/v1/user/context")
    assert response.status_code == 422
