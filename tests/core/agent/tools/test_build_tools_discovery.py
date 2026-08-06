"""build_tools — discovery entitlement gate.

`find_saved` (own library, zero external cost) is always bound; the two
external-provider tools are withheld when `discovery_enabled` is false.
`research` (knowledge-layer read, no external cost) is always bound,
whatever the tier.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from kebi.core.agent.tools import build_tools


def _tool_names(discovery_enabled: bool) -> set[str]:
    tools = build_tools(
        MagicMock(),  # hybrid_search
        MagicMock(),  # candidate_namer
        MagicMock(),  # places_search_factory
        MagicMock(),  # research_service
        discovery_enabled=discovery_enabled,
    )
    return {t.name for t in tools}


def test_discovery_enabled_binds_the_paid_tool() -> None:
    # `discover_places` is no longer bound: its catalog-floor search runs
    # inside `suggest_places` automatically (ADR-140), so it is not a routing
    # choice the model can make or forget.
    names = _tool_names(discovery_enabled=True)
    assert names == {"find_saved", "suggest_places", "research"}
    assert "discover_places" not in names


def test_discovery_disabled_binds_find_saved_and_research() -> None:
    names = _tool_names(discovery_enabled=False)
    assert names == {"find_saved", "research"}


def test_defaults_to_enabled() -> None:
    tools = build_tools(MagicMock(), MagicMock(), MagicMock(), MagicMock())
    assert {t.name for t in tools} == {
        "find_saved",
        "suggest_places",
        "research",
    }


def test_web_search_is_bound_when_the_service_is_wired() -> None:
    tools = build_tools(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        web_knowledge=MagicMock(),
    )
    assert "web_search" in {t.name for t in tools}


def test_web_search_is_ungated_by_the_discovery_tier() -> None:
    """A knowledge question is not place discovery. Withholding the outside
    world from a lower tier would leave that tier answering "when is the
    final" from training weights, which is the failure ADR-145 exists to
    close — for everyone, not just payers."""
    tools = build_tools(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        web_knowledge=MagicMock(),
        discovery_enabled=False,
    )
    names = {t.name for t in tools}
    assert "web_search" in names
    assert "suggest_places" not in names


def test_no_web_service_means_no_web_tool() -> None:
    """Omitted, the agent answers world questions from its own knowledge —
    the pre-ADR-145 behaviour, not a crash."""
    tools = build_tools(MagicMock(), MagicMock(), MagicMock(), MagicMock())
    assert "web_search" not in {t.name for t in tools}
