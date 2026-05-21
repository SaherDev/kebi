"""Tests for build_turn_payload per-turn reset helper (feature 027 M3, FR-022)."""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from kebi.core.agent.invocation import build_turn_payload
from kebi.core.agent.state import LOCATION_INHERIT


class TestBuildTurnPayload:
    def test_resets_transient_fields_on_every_call(self) -> None:
        p1 = build_turn_payload(
            message="first turn",
            user_id="u1",
            taste_profile_summary="",
            memory_summary="",
        )
        p2 = build_turn_payload(
            message="second turn",
            user_id="u1",
            taste_profile_summary="different",
            memory_summary="different",
        )
        assert "last_recall_results" not in p1
        assert p1["reasoning_steps"] == []
        assert "last_recall_results" not in p2
        assert p2["reasoning_steps"] == []

    def test_resets_counters_on_every_call(self) -> None:
        p = build_turn_payload(
            message="hi",
            user_id="u1",
            taste_profile_summary="",
            memory_summary="",
        )
        assert p["steps_taken"] == 0
        assert p["error_count"] == 0

    def test_appends_single_human_message(self) -> None:
        p = build_turn_payload(
            message="hello world",
            user_id="u1",
            taste_profile_summary="",
            memory_summary="",
        )
        assert len(p["messages"]) == 1
        assert isinstance(p["messages"][0], HumanMessage)
        assert p["messages"][0].content == "hello world"

    def test_preserves_user_location_and_summaries(self) -> None:
        loc = {"lat": 13.7, "lng": 100.5}
        p = build_turn_payload(
            message="hi",
            user_id="u1",
            taste_profile_summary="likes ramen",
            memory_summary="vegetarian",
            user_location=loc,
        )
        assert p["taste_profile_summary"] == "likes ramen"
        assert p["memory_summary"] == "vegetarian"
        assert p["user_location"] == loc
        assert p["user_id"] == "u1"

    def test_user_location_defaults_to_none(self) -> None:
        p = build_turn_payload(
            message="hi",
            user_id="u1",
            taste_profile_summary="",
            memory_summary="",
        )
        assert p["user_location"] is None

    def test_passes_inherit_sentinel_to_preserve_carried_working_location(
        self,
    ) -> None:
        """build_turn_payload must pass LOCATION_INHERIT — never a concrete
        working_location — so the prior turn's resolved value carries forward.
        """
        p = build_turn_payload(
            message="hi",
            user_id="u1",
            taste_profile_summary="",
            memory_summary="",
        )
        assert p["working_location"] == LOCATION_INHERIT
        assert p["location_clarification"] is None
