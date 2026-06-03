"""Unit tests for the shared reasoning-step summary phrasing (ADR-103)."""

from __future__ import annotations

from kebi.core.agent.tools._summaries import found_summary


def test_found_summary_caps_preview_and_collapses_rest() -> None:
    names = ["Per Se", "Blue Hill", "Gramercy", "Eleven Madison", "Daniel"]
    assert found_summary(names) == "5 spots — Per Se, Blue Hill, +3 more"


def test_found_summary_singular_no_extra() -> None:
    assert found_summary(["Per Se"]) == "1 spot — Per Se"


def test_found_summary_dropped_tail() -> None:
    assert found_summary(["A", "B"], dropped=2) == "2 spots — A, B (2 didn't fit)"


def test_found_summary_trims_long_google_names() -> None:
    """Long provider names are trimmed in the preview (full names stay in
    `tool_results`): drop the parenthetical tail and cap the length."""
    long_name = (
        "Wagyu Steak & Hamburger (Halal Vegan Gluten free) "
        "Shibuya Restaurant 涩谷 和牛 餐厅"
    )
    summary = found_summary([long_name, "Buvette"])
    # The parenthetical and trailing descriptors are gone; the leading short
    # name survives and the second name is intact.
    assert "Halal" not in summary
    assert "餐厅" not in summary
    assert "Wagyu Steak & Hamburger" in summary
    assert "Buvette" in summary
