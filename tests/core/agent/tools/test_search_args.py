"""Tool-arg coercion shared by the consult-family tools.

The category vocabulary is closed and the model reaches past it ("surf" for
a surf query). Strict validation made that fatal: LangChain validates tool
args before the tool body runs, so one unusable member of an OR-combined
filter list cost the turn its whole retrieval step.
"""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter

from kebi.core.agent.tools._search_args import CategoryArg
from kebi.core.places.models import PlaceCategory

_adapter = TypeAdapter(CategoryArg)


def _agent_state() -> dict[str, Any]:
    """Enough AgentState for the injected-state field to validate."""
    return {
        "messages": [],
        "user_id": "user-1",
        "taste_profile_summary": "",
        "memory_summary": "",
        "user_location": None,
        "working_location": None,
        "location_clarification": None,
        "movement_profile": None,
        "trip_movement": None,
        "user_profile": None,
        "local_time": None,
        "taste_values": [],
        "tool_payloads": [],
        "tool_results": [],
        "reasoning_steps": [],
        "steps_taken": 0,
        "error_count": 0,
        "tool_calls_used": 0,
    }


class TestCategoryArg:
    def test_an_unknown_value_is_dropped_not_fatal(self) -> None:
        assert _adapter.validate_python(["bar", "surf", "beach"]) == [
            PlaceCategory.bar,
            PlaceCategory.beach,
        ]

    def test_all_unknown_reads_as_no_filter(self) -> None:
        # None, not [] — downstream `if not categories` guards must see this
        # exactly as an omitted argument.
        assert _adapter.validate_python(["surf"]) is None

    def test_an_omitted_arg_stays_none(self) -> None:
        assert _adapter.validate_python(None) is None

    def test_known_values_are_untouched(self) -> None:
        assert _adapter.validate_python(["cafe"]) == [PlaceCategory.cafe]

    def test_enum_members_pass_through(self) -> None:
        assert _adapter.validate_python([PlaceCategory.cafe]) == [PlaceCategory.cafe]

    def test_the_schema_still_names_the_vocabulary(self) -> None:
        # Forgiving at runtime must not stop the model being told the real
        # values — that is what keeps the misses rare.
        schema = _adapter.json_schema()
        assert "PlaceCategory" in schema["$defs"]
        assert "restaurant" in schema["$defs"]["PlaceCategory"]["enum"]


class TestToolArgParsing:
    """The regression itself: LangChain validates tool args before the tool
    body runs, so an out-of-vocabulary category used to raise inside
    `_parse_input` and take the whole tool call — and the turn's retrieval —
    with it."""

    def _tool(self) -> Any:
        from unittest.mock import MagicMock

        from kebi.core.agent.tools.suggest_places_tool import (
            build_suggest_places_tool,
        )

        return build_suggest_places_tool(
            namer=MagicMock(),
            places_search_factory=MagicMock(),
            notes_service=MagicMock(),
        )

    def _parse(self, categories: list[str]) -> Any:
        _, kwargs = self._tool()._to_args_and_kwargs(
            {"query": "surf bars", "categories": categories, "state": _agent_state()},
            "call-1",
        )
        return kwargs["categories"]

    def test_a_surf_query_no_longer_kills_the_call(self) -> None:
        assert self._parse(["bar", "surf"]) == [PlaceCategory.bar]

    def test_an_all_unknown_list_parses_as_no_filter(self) -> None:
        assert self._parse(["surf"]) is None
