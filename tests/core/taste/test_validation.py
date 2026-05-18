"""Tests for validate_grounded and format_summary_for_agent (ADR-077)."""

from __future__ import annotations

import pytest

from kebi.core.taste.aggregation import (
    SignalCounts,
    TagCounts,
    TotalCounts,
)
from kebi.core.taste.regen import format_summary_for_agent, validate_grounded
from kebi.core.taste.schemas import SummaryLine, TasteArtifacts


@pytest.fixture()
def signal_counts() -> SignalCounts:
    return SignalCounts(
        totals=TotalCounts(saves=10),
        categories={"restaurant": 6, "cafe": 4},
        source={"tiktok": 7},
        tags=TagCounts(cuisine={"japanese": 8, "italian": 5}),
    )


# ---------------------------------------------------------------------------
# validate_grounded — summary lines
# ---------------------------------------------------------------------------


def test_valid_summary_line_passes(signal_counts: SignalCounts) -> None:
    line = SummaryLine(
        text="Loves Japanese food",
        signal_count=8,
        source_field="tags.cuisine",
        source_value="japanese",
    )
    artifacts = TasteArtifacts(summary=[line])
    validated, dropped = validate_grounded(artifacts, signal_counts)

    assert len(validated.summary) == 1
    assert validated.summary[0].text == "Loves Japanese food"
    assert dropped == []


def test_grounded_category_passes(signal_counts: SignalCounts) -> None:
    line = SummaryLine(
        text="Favors restaurant category",
        signal_count=6,
        source_field="categories",
        source_value="restaurant",
    )
    validated, dropped = validate_grounded(
        TasteArtifacts(summary=[line]), signal_counts
    )
    assert len(validated.summary) == 1
    assert dropped == []


def test_bad_source_field_drops_summary(signal_counts: SignalCounts) -> None:
    line = SummaryLine(
        text="Loves Mexican food",
        signal_count=3,
        source_field="tags.nonexistent",
        source_value="mexican",
    )
    artifacts = TasteArtifacts(summary=[line])
    validated, dropped = validate_grounded(artifacts, signal_counts)

    assert len(validated.summary) == 0
    assert len(dropped) == 1
    assert dropped[0]["type"] == "summary"
    assert dropped[0]["source_field"] == "tags.nonexistent"


def test_null_source_value_for_aggregate_passes(signal_counts: SignalCounts) -> None:
    """SummaryLine with source_value=None passes if the path exists."""
    line = SummaryLine(
        text="Saves lots of places",
        signal_count=10,
        source_field="totals",
        source_value=None,
    )
    artifacts = TasteArtifacts(summary=[line])
    validated, dropped = validate_grounded(artifacts, signal_counts)

    assert len(validated.summary) == 1
    assert dropped == []


def test_ungrounded_value_dropped(signal_counts: SignalCounts) -> None:
    """Path exists but the value isn't present → dropped."""
    line = SummaryLine(
        text="Loves bar category",
        signal_count=3,
        source_field="categories",
        source_value="bar",
    )
    validated, dropped = validate_grounded(
        TasteArtifacts(summary=[line]), signal_counts
    )
    assert validated.summary == []
    assert len(dropped) == 1


def test_all_items_dropped_returns_empty(signal_counts: SignalCounts) -> None:
    line = SummaryLine(
        text="Bad line",
        signal_count=3,
        source_field="nonexistent.path",
        source_value="x",
    )
    artifacts = TasteArtifacts(summary=[line])
    validated, dropped = validate_grounded(artifacts, signal_counts)

    assert validated.summary == []
    assert len(dropped) == 1


# ---------------------------------------------------------------------------
# format_summary_for_agent
# ---------------------------------------------------------------------------


def test_format_summary_for_agent_joins_lines() -> None:
    lines = [
        SummaryLine(
            text="Loves Japanese food",
            signal_count=8,
            source_field="tags.cuisine",
            source_value="japanese",
        ),
        SummaryLine(
            text="Favors restaurant category",
            signal_count=6,
            source_field="categories",
            source_value="restaurant",
        ),
    ]
    result = format_summary_for_agent(lines)

    assert result == (
        "- Loves Japanese food [8 signals]\n"
        "- Favors restaurant category [6 signals]"
    )


def test_format_summary_for_agent_empty() -> None:
    assert format_summary_for_agent([]) == ""
