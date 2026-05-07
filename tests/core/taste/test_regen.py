"""Tests for kebi.core.taste.regen prompt builder functions."""

from __future__ import annotations

import json

import kebi.core.taste.regen as regen_mod
from kebi.core.taste.aggregation import SignalCounts, TotalCounts
from kebi.core.taste.regen import (
    build_regen_messages,
    format_summary_for_agent,
    load_regen_prompt_template,
)
from kebi.core.taste.schemas import SummaryLine


def _make_signal_counts() -> SignalCounts:
    return SignalCounts(totals=TotalCounts(saves=5))


def test_load_regen_prompt_template() -> None:
    # Clear cache to ensure fresh load
    regen_mod._prompt_cache = None
    result = load_regen_prompt_template()
    assert isinstance(result, str)
    assert len(result) > 0
    assert "signal counts" in result.lower()


def test_build_regen_messages_structure() -> None:
    counts = _make_signal_counts()
    messages = build_regen_messages(signal_counts=counts, early_signal_threshold=10)
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"


def test_build_regen_messages_contains_signal_counts() -> None:
    counts = _make_signal_counts()
    messages = build_regen_messages(signal_counts=counts, early_signal_threshold=10)
    user_content = messages[1]["content"]
    parsed = json.loads(user_content)
    assert parsed["totals"]["saves"] == 5


def test_build_regen_messages_replaces_threshold() -> None:
    counts = _make_signal_counts()
    threshold = 42
    messages = build_regen_messages(
        signal_counts=counts,
        early_signal_threshold=threshold,
    )
    system_content = messages[0]["content"]
    assert "{early_signal_threshold}" not in system_content
    assert str(threshold) in system_content


def test_format_summary_for_agent() -> None:
    lines = [
        SummaryLine(
            text="Loves Italian food",
            signal_count=7,
            source_field="attributes.cuisine",
            source_value="italian",
        ),
        SummaryLine(
            text="Prefers casual vibes",
            signal_count=3,
            source_field="attributes.ambiance",
            source_value="casual",
        ),
    ]
    result = format_summary_for_agent(lines)
    assert "- Loves Italian food [7 signals]" in result
    assert "- Prefers casual vibes [3 signals]" in result
    assert result.count("\n") == 1  # two lines joined by one newline
