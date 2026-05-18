"""Tests for build_regen_messages (summary-only, ADR-076)."""

from __future__ import annotations

import json

from kebi.core.taste.aggregation import SignalCounts
from kebi.core.taste.regen import build_regen_messages


def test_returns_system_and_user_messages() -> None:
    messages = build_regen_messages(SignalCounts(), early_signal_threshold=10)

    assert [m["role"] for m in messages] == ["system", "user"]


def test_user_message_is_serialized_signal_counts() -> None:
    counts = SignalCounts()
    counts.source["tiktok"] = 4

    messages = build_regen_messages(counts, early_signal_threshold=10)

    body = json.loads(messages[1]["content"])
    assert body["source"]["tiktok"] == 4
    # Chip arrays no longer exist in the prompt body (ADR-076).
    assert "confirmed_chips" not in body
    assert "rejected_chips" not in body


def test_early_signal_threshold_substituted_into_system_prompt() -> None:
    messages = build_regen_messages(SignalCounts(), early_signal_threshold=42)

    system = messages[0]["content"]
    assert "42" in system
    assert "{early_signal_threshold}" not in system
