"""build_tools — discovery entitlement gate.

`find_saved` (own library, zero external cost) is always bound; the two
external-provider tools are withheld when `discovery_enabled` is false.
`research` and `suggest_areas` (knowledge/geocode reads, no provider place
call) are always bound, whatever the tier — a lower tier should still be able
to be told *where* to go, even without venues pinned inside.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from kebi.core.agent.tools import build_tools

_ALL = {
    "find_saved",
    "suggest_places",
    "discover_places",
    "suggest_areas",
    "research",
}


def _tool_names(discovery_enabled: bool) -> set[str]:
    tools = build_tools(
        MagicMock(),  # hybrid_search
        MagicMock(),  # candidate_namer
        MagicMock(),  # places_search_factory
        MagicMock(),  # research_service
        MagicMock(),  # area_suggestion_service
        discovery_enabled=discovery_enabled,
    )
    return {t.name for t in tools}


def test_discovery_enabled_binds_every_tool() -> None:
    assert _tool_names(discovery_enabled=True) == _ALL


def test_discovery_disabled_withholds_only_the_provider_tools() -> None:
    assert _tool_names(discovery_enabled=False) == {
        "find_saved",
        "suggest_areas",
        "research",
    }


def test_defaults_to_enabled() -> None:
    tools = build_tools(MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())
    assert {t.name for t in tools} == _ALL
