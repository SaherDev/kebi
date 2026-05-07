"""Unit tests for SignalService chip_confirm path (feature 023)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.api.schemas.signal import ChipConfirmChipItem, ChipConfirmMetadata
from kebi.core.events.events import ChipConfirmed
from kebi.core.signal.service import SignalService
from kebi.core.taste.schemas import Chip, ChipStatus, TasteProfile
from kebi.db.models import InteractionType


def _metadata(status_for_ramen: str = "confirmed") -> ChipConfirmMetadata:
    return ChipConfirmMetadata(
        chips=[
            ChipConfirmChipItem(
                label="Ramen lover",
                signal_count=3,
                source_field="attributes.cuisine",
                source_value="ramen",
                status=status_for_ramen,  # type: ignore[arg-type]
                selection_round="round_1",
            )
        ],
    )


def _profile(chips: list[Chip]) -> TasteProfile:
    return TasteProfile(
        taste_profile_summary=[],
        signal_counts={"totals": {"saves": 5}},
        chips=chips,
        generated_from_log_count=5,
    )


def _build_service(
    *,
    existing_chips: list[Chip] | None = None,
) -> tuple[SignalService, AsyncMock, MagicMock, AsyncMock]:
    """Return (service, repo_mock, dispatcher_mock, taste_service_mock)."""
    repo = AsyncMock()
    repo.log_interaction = AsyncMock()
    repo.merge_chip_statuses = AsyncMock()

    taste_service = AsyncMock()
    taste_service._repo = repo
    if existing_chips is None:
        taste_service.get_taste_profile = AsyncMock(return_value=None)
    else:
        taste_service.get_taste_profile = AsyncMock(
            return_value=_profile(existing_chips)
        )

    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()

    session_factory = MagicMock()  # unused in chip_confirm path

    service = SignalService(
        session_factory=session_factory,
        event_dispatcher=dispatcher,
        taste_service=taste_service,
    )
    return service, repo, dispatcher, taste_service


@pytest.mark.asyncio
async def test_chip_confirm_writes_interaction_row_with_metadata() -> None:
    existing = [
        Chip(
            label="Ramen",
            source_field="attributes.cuisine",
            source_value="ramen",
            signal_count=3,
            status=ChipStatus.PENDING,
        )
    ]
    service, repo, dispatcher, _ = _build_service(existing_chips=existing)
    meta = _metadata()

    await service.handle_signal(
        signal_type="chip_confirm", user_id="user_abc", chip_metadata=meta
    )

    repo.log_interaction.assert_awaited_once()
    kwargs = repo.log_interaction.await_args.kwargs
    assert kwargs["interaction_type"] == InteractionType.CHIP_CONFIRM
    assert kwargs["place_id"] is None
    assert kwargs["metadata"] == meta.model_dump()


@pytest.mark.asyncio
async def test_chip_confirm_merges_statuses_and_persists() -> None:
    existing = [
        Chip(
            label="Ramen",
            source_field="attributes.cuisine",
            source_value="ramen",
            signal_count=3,
            status=ChipStatus.PENDING,
        )
    ]
    service, repo, dispatcher, _ = _build_service(existing_chips=existing)

    await service.handle_signal(
        signal_type="chip_confirm",
        user_id="user_abc",
        chip_metadata=_metadata(status_for_ramen="confirmed"),
    )

    repo.merge_chip_statuses.assert_awaited_once()
    passed = repo.merge_chip_statuses.await_args.kwargs
    assert passed["user_id"] == "user_abc"
    assert len(passed["updated_chips"]) == 1
    assert passed["updated_chips"][0]["status"] == "confirmed"
    assert passed["updated_chips"][0]["selection_round"] == "round_1"


@pytest.mark.asyncio
async def test_chip_confirm_dispatches_chip_confirmed_event() -> None:
    service, _, dispatcher, _ = _build_service(existing_chips=[])

    await service.handle_signal(
        signal_type="chip_confirm", user_id="user_abc", chip_metadata=_metadata()
    )

    dispatcher.dispatch.assert_awaited_once()
    event = dispatcher.dispatch.await_args.args[0]
    assert isinstance(event, ChipConfirmed)
    assert event.user_id == "user_abc"


@pytest.mark.asyncio
async def test_chip_confirm_with_unknown_chip_is_noop_for_that_chip() -> None:
    existing = [
        Chip(
            label="TikTok",
            source_field="source",
            source_value="tiktok",
            signal_count=5,
            status=ChipStatus.PENDING,
        )
    ]
    service, repo, _, _ = _build_service(existing_chips=existing)

    await service.handle_signal(
        signal_type="chip_confirm",
        user_id="user_abc",
        chip_metadata=_metadata(),  # targets "ramen" which isn't in existing
    )

    passed = repo.merge_chip_statuses.await_args.kwargs
    chips = passed["updated_chips"]
    assert len(chips) == 1
    # tiktok preserved with original status, ramen not added
    assert chips[0]["source_value"] == "tiktok"
    assert chips[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_chip_confirm_without_existing_profile_skips_persist() -> None:
    service, repo, dispatcher, _ = _build_service(existing_chips=None)

    await service.handle_signal(
        signal_type="chip_confirm", user_id="user_abc", chip_metadata=_metadata()
    )

    # Interaction row is still written; event still dispatched.
    repo.log_interaction.assert_awaited_once()
    dispatcher.dispatch.assert_awaited_once()
    # But chip merge persistence is skipped (no taste_model row).
    repo.merge_chip_statuses.assert_not_awaited()


@pytest.mark.asyncio
async def test_chip_confirm_missing_metadata_raises_value_error() -> None:
    service, _, _, _ = _build_service(existing_chips=[])

    with pytest.raises(ValueError, match="chip_confirm signal requires chip_metadata"):
        await service.handle_signal(signal_type="chip_confirm", user_id="user_abc")
