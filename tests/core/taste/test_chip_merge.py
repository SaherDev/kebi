"""Unit tests for chip_merge helpers (feature 023)."""

from __future__ import annotations

from kebi.api.schemas.signal import ChipConfirmChipItem
from kebi.core.taste.chip_merge import (
    merge_chip_statuses,
    merge_chips_after_regen,
)
from kebi.core.taste.schemas import Chip, ChipStatus


def _chip(
    source_field: str = "source",
    source_value: str = "tiktok",
    signal_count: int = 3,
    status: ChipStatus = ChipStatus.PENDING,
    selection_round: str | None = None,
    label: str | None = None,
) -> Chip:
    return Chip(
        label=label or f"{source_value} chip",
        source_field=source_field,
        source_value=source_value,
        signal_count=signal_count,
        status=status,
        selection_round=selection_round,
    )


def _submission(
    source_field: str,
    source_value: str,
    status: str,
    selection_round: str = "round_1",
    signal_count: int = 3,
) -> ChipConfirmChipItem:
    return ChipConfirmChipItem(
        label=f"{source_value} label",
        signal_count=signal_count,
        source_field=source_field,
        source_value=source_value,
        status=status,  # type: ignore[arg-type]
        selection_round=selection_round,
    )


# -------------------- merge_chip_statuses (signal handler path) --------------------


def test_confirms_pending_chip_with_submission() -> None:
    existing = [_chip(source_field="source", source_value="tiktok")]
    submissions = [_submission("source", "tiktok", "confirmed")]

    result = merge_chip_statuses(existing, submissions)

    assert len(result) == 1
    assert result[0].status == ChipStatus.CONFIRMED
    assert result[0].selection_round == "round_1"


def test_rejects_pending_chip_with_submission() -> None:
    existing = [_chip(source_field="attributes.vibe", source_value="casual")]
    submissions = [_submission("attributes.vibe", "casual", "rejected")]

    result = merge_chip_statuses(existing, submissions)

    assert result[0].status == ChipStatus.REJECTED
    assert result[0].selection_round == "round_1"


def test_already_confirmed_chip_is_untouched() -> None:
    existing = [
        _chip(
            source_field="attributes.cuisine",
            source_value="ramen",
            status=ChipStatus.CONFIRMED,
            selection_round="round_1",
        )
    ]
    submissions = [
        _submission(
            "attributes.cuisine", "ramen", "rejected", selection_round="round_2"
        )
    ]

    result = merge_chip_statuses(existing, submissions)

    assert result[0].status == ChipStatus.CONFIRMED
    assert result[0].selection_round == "round_1"


def test_submission_without_matching_existing_chip_is_ignored() -> None:
    existing = [_chip(source_field="source", source_value="tiktok")]
    submissions = [_submission("attributes.cuisine", "ramen", "confirmed")]

    result = merge_chip_statuses(existing, submissions)

    # Stored chip unchanged; non-matching submission silently ignored.
    assert len(result) == 1
    assert result[0].source_value == "tiktok"
    assert result[0].status == ChipStatus.PENDING


def test_existing_chip_not_in_submission_is_preserved() -> None:
    existing = [
        _chip(source_field="source", source_value="tiktok"),
        _chip(source_field="attributes.cuisine", source_value="ramen"),
    ]
    submissions = [_submission("source", "tiktok", "confirmed")]

    result = merge_chip_statuses(existing, submissions)

    ramen = next(c for c in result if c.source_value == "ramen")
    assert ramen.status == ChipStatus.PENDING
    assert ramen.selection_round is None


def test_rejected_chip_can_be_re_decided_by_later_submission() -> None:
    # Contrast with confirmed — a rejected chip in the existing array is
    # overwritten by a submission, unlike confirmed which is immutable.
    existing = [
        _chip(
            source_field="attributes.vibe",
            source_value="casual",
            status=ChipStatus.REJECTED,
            selection_round="round_1",
        )
    ]
    submissions = [
        _submission("attributes.vibe", "casual", "confirmed", selection_round="round_2")
    ]

    result = merge_chip_statuses(existing, submissions)

    assert result[0].status == ChipStatus.CONFIRMED
    assert result[0].selection_round == "round_2"


# -------------------- merge_chips_after_regen (regen path) --------------------


def test_regen_preserves_confirmed_chip_even_if_missing_from_fresh() -> None:
    existing = [
        _chip(
            source_field="attributes.cuisine",
            source_value="ramen",
            status=ChipStatus.CONFIRMED,
            selection_round="round_1",
            signal_count=5,
        )
    ]
    fresh: list[Chip] = []  # LLM dropped the chip

    result = merge_chips_after_regen(existing, fresh)

    assert len(result) == 1
    assert result[0].status == ChipStatus.CONFIRMED
    assert result[0].selection_round == "round_1"


def test_regen_resurfaces_rejected_when_signal_grows() -> None:
    existing = [
        _chip(
            source_field="attributes.cuisine",
            source_value="ramen",
            status=ChipStatus.REJECTED,
            selection_round="round_1",
            signal_count=3,
        )
    ]
    fresh = [
        _chip(
            source_field="attributes.cuisine",
            source_value="ramen",
            signal_count=5,
        )
    ]

    result = merge_chips_after_regen(existing, fresh)

    assert result[0].status == ChipStatus.PENDING
    assert result[0].selection_round is None
    assert result[0].signal_count == 5


def test_regen_keeps_rejected_when_signal_does_not_grow() -> None:
    existing = [
        _chip(
            source_field="attributes.cuisine",
            source_value="ramen",
            status=ChipStatus.REJECTED,
            selection_round="round_1",
            signal_count=4,
        )
    ]
    fresh = [
        _chip(
            source_field="attributes.cuisine",
            source_value="ramen",
            signal_count=4,  # equal, not greater
        )
    ]

    result = merge_chips_after_regen(existing, fresh)

    assert result[0].status == ChipStatus.REJECTED
    assert result[0].selection_round == "round_1"


def test_regen_updates_pending_signal_count() -> None:
    existing = [
        _chip(
            source_field="source",
            source_value="tiktok",
            status=ChipStatus.PENDING,
            signal_count=3,
        )
    ]
    fresh = [_chip(source_field="source", source_value="tiktok", signal_count=7)]

    result = merge_chips_after_regen(existing, fresh)

    assert result[0].status == ChipStatus.PENDING
    assert result[0].signal_count == 7


def test_regen_adds_new_fresh_chip_as_pending() -> None:
    existing: list[Chip] = []
    fresh = [_chip(source_field="source", source_value="tiktok", signal_count=3)]

    result = merge_chips_after_regen(existing, fresh)

    assert len(result) == 1
    assert result[0].status == ChipStatus.PENDING
    assert result[0].selection_round is None
