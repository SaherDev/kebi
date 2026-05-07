"""Tests for build_regen_messages with chip-status input (feature 023)."""

from __future__ import annotations

import json

from kebi.core.taste.aggregation import SignalCounts
from kebi.core.taste.regen import build_regen_messages
from kebi.core.taste.schemas import Chip, ChipStatus


def _chip(
    status: ChipStatus,
    source_field: str = "attributes.cuisine",
    source_value: str = "ramen",
) -> Chip:
    return Chip(
        label=f"{source_value} chip",
        source_field=source_field,
        source_value=source_value,
        signal_count=3,
        status=status,
    )


def test_baseline_prompt_omits_chip_keys_when_no_confirmed_or_rejected() -> None:
    counts = SignalCounts()
    messages = build_regen_messages(
        counts, early_signal_threshold=10, existing_chips=None
    )

    user_body = json.loads(messages[1]["content"])
    assert "confirmed_chips" not in user_body
    assert "rejected_chips" not in user_body


def test_empty_existing_chips_list_omits_chip_keys() -> None:
    counts = SignalCounts()
    messages = build_regen_messages(
        counts, early_signal_threshold=10, existing_chips=[]
    )

    user_body = json.loads(messages[1]["content"])
    assert "confirmed_chips" not in user_body
    assert "rejected_chips" not in user_body


def test_pending_chips_only_still_omits_chip_keys() -> None:
    counts = SignalCounts()
    messages = build_regen_messages(
        counts,
        early_signal_threshold=10,
        existing_chips=[_chip(ChipStatus.PENDING)],
    )

    user_body = json.loads(messages[1]["content"])
    assert "confirmed_chips" not in user_body
    assert "rejected_chips" not in user_body


def test_confirmed_chip_serialized_as_input_array() -> None:
    counts = SignalCounts()
    messages = build_regen_messages(
        counts,
        early_signal_threshold=10,
        existing_chips=[
            _chip(ChipStatus.CONFIRMED, source_field="source", source_value="tiktok")
        ],
    )

    user_body = json.loads(messages[1]["content"])
    assert user_body["confirmed_chips"] == [
        {"source_field": "source", "source_value": "tiktok"}
    ]
    assert "rejected_chips" not in user_body


def test_rejected_chip_serialized_as_input_array() -> None:
    counts = SignalCounts()
    messages = build_regen_messages(
        counts,
        early_signal_threshold=10,
        existing_chips=[
            _chip(
                ChipStatus.REJECTED,
                source_field="attributes.vibe",
                source_value="casual",
            )
        ],
    )

    user_body = json.loads(messages[1]["content"])
    assert user_body["rejected_chips"] == [
        {"source_field": "attributes.vibe", "source_value": "casual"}
    ]
    assert "confirmed_chips" not in user_body


def test_mixed_chips_serialize_separately() -> None:
    counts = SignalCounts()
    chips = [
        _chip(
            ChipStatus.CONFIRMED,
            source_field="attributes.cuisine",
            source_value="ramen",
        ),
        _chip(
            ChipStatus.REJECTED,
            source_field="attributes.vibe",
            source_value="casual",
        ),
        _chip(ChipStatus.PENDING, source_field="source", source_value="tiktok"),
    ]
    messages = build_regen_messages(
        counts, early_signal_threshold=10, existing_chips=chips
    )

    user_body = json.loads(messages[1]["content"])
    assert user_body["confirmed_chips"] == [
        {"source_field": "attributes.cuisine", "source_value": "ramen"}
    ]
    assert user_body["rejected_chips"] == [
        {"source_field": "attributes.vibe", "source_value": "casual"}
    ]
    # Pending chips are not serialized to the prompt — the LLM sees them
    # only as signal_counts entries (feature 023 spec).


def test_system_prompt_contains_chip_status_rules() -> None:
    counts = SignalCounts()
    messages = build_regen_messages(
        counts, early_signal_threshold=10, existing_chips=[]
    )

    system = messages[0]["content"]
    # Confirmed path still produces summary sentences with [confirmed].
    assert "confirmed_chips" in system
    assert "[confirmed]" in system
    # Rejected chips are deliberately excluded from the summary to avoid
    # ambiguous double-negative sentences ("not interested in X" + "[rejected]").
    assert "rejected_chips" in system
    assert "MUST NOT appear in the summary" in system


# -------------------- prune-decided-chips-from-signal_counts --------------------


def _counts_with_bangkok() -> SignalCounts:
    """Build a SignalCounts with Bangkok in attributes.location_context.city."""
    counts = SignalCounts()
    counts.source["tiktok"] = 4
    counts.attributes.location_context.city["Bangkok"] = 3
    counts.attributes.cuisine["thai"] = 2
    return counts


def test_rejected_chip_is_pruned_from_signal_counts_in_llm_body() -> None:
    counts = _counts_with_bangkok()
    rejected_bangkok = Chip(
        label="Bangkok regular",
        source_field="attributes.location_context.city",
        source_value="Bangkok",
        signal_count=3,
        status=ChipStatus.REJECTED,
    )
    messages = build_regen_messages(
        counts, early_signal_threshold=10, existing_chips=[rejected_bangkok]
    )

    body = json.loads(messages[1]["content"])
    # Bangkok must not appear in the positive behavioral counts anymore.
    assert "Bangkok" not in body["attributes"]["location_context"]["city"]
    # But it must appear in rejected_chips so the LLM knows to emit a
    # negative [rejected] sentence for it.
    assert body["rejected_chips"] == [
        {"source_field": "attributes.location_context.city", "source_value": "Bangkok"}
    ]
    # Unrelated signals untouched.
    assert body["source"]["tiktok"] == 4
    assert body["attributes"]["cuisine"]["thai"] == 2


def test_confirmed_chip_is_pruned_from_signal_counts_in_llm_body() -> None:
    counts = _counts_with_bangkok()
    confirmed_tiktok = Chip(
        label="TikTok lover",
        source_field="source",
        source_value="tiktok",
        signal_count=4,
        status=ChipStatus.CONFIRMED,
    )
    messages = build_regen_messages(
        counts, early_signal_threshold=10, existing_chips=[confirmed_tiktok]
    )

    body = json.loads(messages[1]["content"])
    assert "tiktok" not in body["source"]
    assert body["confirmed_chips"] == [
        {"source_field": "source", "source_value": "tiktok"}
    ]


def test_pending_chip_is_not_pruned_from_signal_counts() -> None:
    counts = _counts_with_bangkok()
    pending_thai = Chip(
        label="Thai lover",
        source_field="attributes.cuisine",
        source_value="thai",
        signal_count=2,
        status=ChipStatus.PENDING,
    )
    messages = build_regen_messages(
        counts, early_signal_threshold=10, existing_chips=[pending_thai]
    )

    body = json.loads(messages[1]["content"])
    # Pending chips stay in the behavioral tree — they haven't been decided.
    assert body["attributes"]["cuisine"]["thai"] == 2
    # No confirmed_chips/rejected_chips for purely-pending chips.
    assert "confirmed_chips" not in body
    assert "rejected_chips" not in body


def test_prune_is_robust_to_missing_paths() -> None:
    counts = SignalCounts()  # empty
    rejected = Chip(
        label="Bangkok",
        source_field="attributes.location_context.city",
        source_value="Bangkok",
        signal_count=3,
        status=ChipStatus.REJECTED,
    )
    # Should not raise even though the path doesn't exist in the empty body.
    messages = build_regen_messages(
        counts, early_signal_threshold=10, existing_chips=[rejected]
    )
    body = json.loads(messages[1]["content"])
    assert body["rejected_chips"] == [
        {"source_field": "attributes.location_context.city", "source_value": "Bangkok"}
    ]
