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
