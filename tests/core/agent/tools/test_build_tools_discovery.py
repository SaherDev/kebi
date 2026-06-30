"""build_tools — discovery entitlement gate.

`find_saved` (own library, zero external cost) is always bound; the two
external-provider tools are withheld when `discovery_enabled` is false.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from kebi.core.agent.tools import build_tools


def _tool_names(discovery_enabled: bool) -> set[str]:
    tools = build_tools(
        MagicMock(),  # hybrid_search
        MagicMock(),  # candidate_namer
        MagicMock(),  # places_search_factory
        discovery_enabled=discovery_enabled,
    )
    return {t.name for t in tools}


def test_discovery_enabled_binds_all_three() -> None:
    names = _tool_names(discovery_enabled=True)
    assert names == {"find_saved", "suggest_places", "discover_places"}


def test_discovery_disabled_binds_only_find_saved() -> None:
    names = _tool_names(discovery_enabled=False)
    assert names == {"find_saved"}


def test_defaults_to_enabled() -> None:
    tools = build_tools(MagicMock(), MagicMock(), MagicMock())
    assert {t.name for t in tools} == {
        "find_saved",
        "suggest_places",
        "discover_places",
    }
