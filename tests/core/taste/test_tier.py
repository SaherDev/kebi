"""Unit tests for derive_signal_tier (feature 023).

Config-driven: no stage names are hardcoded in the function. Tests exercise
every case from the feature spec's verify list and confirm that adding a new
stage to the stages dict requires zero code changes in tier.py.
"""

from __future__ import annotations

import pytest

from kebi.core.taste.schemas import Chip, ChipStatus
from kebi.core.taste.tier import derive_signal_tier, selection_round_name

STAGES_2 = {"round_1": 5, "round_2": 50}
STAGES_3 = {"round_1": 5, "round_2": 20, "round_3": 50}
CHIP_THRESHOLD = 2


def _chip(
    label: str = "label",
    source_field: str = "source",
    source_value: str = "tiktok",
    signal_count: int = 3,
    status: ChipStatus = ChipStatus.PENDING,
    selection_round: str | None = None,
) -> Chip:
    return Chip(
        label=label,
        source_field=source_field,
        source_value=source_value,
        signal_count=signal_count,
        status=status,
        selection_round=selection_round,
    )


def test_cold_when_signal_count_zero() -> None:
    assert derive_signal_tier(0, [], STAGES_2, CHIP_THRESHOLD) == "cold"


def test_cold_stays_cold_even_with_pending_chips() -> None:
    # Edge: chips array non-empty but count is 0 -> still cold
    assert (
        derive_signal_tier(0, [_chip(signal_count=3)], STAGES_2, CHIP_THRESHOLD)
        == "cold"
    )


def test_warming_below_first_stage() -> None:
    assert derive_signal_tier(3, [], STAGES_2, CHIP_THRESHOLD) == "warming"


def test_warming_just_below_threshold() -> None:
    assert derive_signal_tier(4, [], STAGES_2, CHIP_THRESHOLD) == "warming"


def test_chip_selection_at_round_1_with_pending_chip() -> None:
    chips = [_chip(signal_count=3, status=ChipStatus.PENDING)]
    assert derive_signal_tier(5, chips, STAGES_2, CHIP_THRESHOLD) == "chip_selection"


def test_active_at_round_1_when_every_chip_has_selection_round() -> None:
    chips = [
        _chip(
            signal_count=3,
            status=ChipStatus.CONFIRMED,
            selection_round="round_1",
        ),
        _chip(
            signal_count=3,
            status=ChipStatus.REJECTED,
            selection_round="round_1",
            source_value="other",
        ),
    ]
    assert derive_signal_tier(5, chips, STAGES_2, CHIP_THRESHOLD) == "active"


def test_chip_selection_at_round_2_with_new_pending_chip() -> None:
    chips = [
        # Already-decided from a prior round
        _chip(
            signal_count=12,
            status=ChipStatus.CONFIRMED,
            selection_round="round_1",
        ),
        # Fresh pending chip crossed the threshold
        _chip(
            signal_count=4,
            status=ChipStatus.PENDING,
            selection_round=None,
            source_value="new",
        ),
    ]
    assert derive_signal_tier(50, chips, STAGES_2, CHIP_THRESHOLD) == "chip_selection"


def test_active_at_round_2_with_no_actionable_pending() -> None:
    chips = [
        _chip(
            signal_count=12,
            status=ChipStatus.CONFIRMED,
            selection_round="round_1",
        ),
    ]
    assert derive_signal_tier(50, chips, STAGES_2, CHIP_THRESHOLD) == "active"


def test_pending_chip_below_chip_threshold_is_not_actionable() -> None:
    # signal_count=1 < chip_threshold=2 -> chip is pending but not yet shown
    chips = [_chip(signal_count=1, status=ChipStatus.PENDING)]
    assert derive_signal_tier(5, chips, STAGES_2, CHIP_THRESHOLD) == "active"


def test_pending_chip_with_selection_round_set_is_not_actionable() -> None:
    # selection_round populated -> treated as already offered
    chips = [
        _chip(
            signal_count=3,
            status=ChipStatus.PENDING,
            selection_round="round_1",
        )
    ]
    assert derive_signal_tier(5, chips, STAGES_2, CHIP_THRESHOLD) == "active"


def test_adding_round_3_requires_no_code_changes() -> None:
    """The user's core requirement: extend stages dict, function keeps working."""
    # Count between round_2 and round_3 with actionable pending -> chip_selection
    chips = [_chip(signal_count=3, status=ChipStatus.PENDING)]
    assert derive_signal_tier(30, chips, STAGES_3, CHIP_THRESHOLD) == "chip_selection"
    # Count at round_3 threshold with no actionable pending -> active
    assert (
        derive_signal_tier(
            50,
            [
                _chip(
                    signal_count=6,
                    status=ChipStatus.CONFIRMED,
                    selection_round="round_2",
                )
            ],
            STAGES_3,
            CHIP_THRESHOLD,
        )
        == "active"
    )


def test_unordered_stages_dict_still_works() -> None:
    # Function must not depend on insertion order of stages dict.
    unordered = {"round_2": 20, "round_3": 50, "round_1": 5}
    chips = [_chip(signal_count=3, status=ChipStatus.PENDING)]
    assert derive_signal_tier(5, chips, unordered, CHIP_THRESHOLD) == "chip_selection"


def test_stage_names_are_not_special_cased() -> None:
    # Arbitrary stage names work just as well as "round_N".
    custom = {"tier_a": 5, "tier_b": 40}
    assert derive_signal_tier(4, [], custom, CHIP_THRESHOLD) == "warming"
    assert (
        derive_signal_tier(
            6,
            [_chip(signal_count=3, status=ChipStatus.PENDING)],
            custom,
            CHIP_THRESHOLD,
        )
        == "chip_selection"
    )


@pytest.mark.parametrize(
    "count,expected",
    [
        (0, "cold"),
        (4, "warming"),
        (5, "active"),  # no chips at all -> no actionable pending
        (20, "active"),
        (50, "active"),
        (1000, "active"),
    ],
)
def test_no_chips_at_any_count(count: int, expected: str) -> None:
    assert derive_signal_tier(count, [], STAGES_3, CHIP_THRESHOLD) == expected


# -------------------- selection_round_name --------------------


def test_selection_round_name_none_when_no_stage_crossed() -> None:
    assert selection_round_name(0, STAGES_3) is None
    assert selection_round_name(4, STAGES_3) is None


def test_selection_round_name_returns_highest_crossed_stage() -> None:
    assert selection_round_name(5, STAGES_3) == "round_1"
    assert selection_round_name(19, STAGES_3) == "round_1"
    assert selection_round_name(20, STAGES_3) == "round_2"
    assert selection_round_name(49, STAGES_3) == "round_2"
    assert selection_round_name(50, STAGES_3) == "round_3"
    assert selection_round_name(1000, STAGES_3) == "round_3"


def test_selection_round_name_works_with_two_stage_config() -> None:
    assert selection_round_name(5, STAGES_2) == "round_1"
    assert selection_round_name(50, STAGES_2) == "round_2"


def test_selection_round_name_is_insensitive_to_dict_order() -> None:
    # Stage dict in arbitrary order — the helper sorts by threshold value.
    unordered = {"round_3": 50, "round_1": 5, "round_2": 20}
    assert selection_round_name(20, unordered) == "round_2"
    assert selection_round_name(50, unordered) == "round_3"


def test_selection_round_name_accepts_arbitrary_stage_names() -> None:
    custom = {"tier_a": 5, "tier_b": 40}
    assert selection_round_name(10, custom) == "tier_a"
    assert selection_round_name(40, custom) == "tier_b"
