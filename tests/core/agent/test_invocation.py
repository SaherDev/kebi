"""Tests for build_turn_payload per-turn reset helper (feature 027 M3, FR-022)."""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from kebi.core.agent.invocation import build_turn_payload


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
        assert p1["last_recall_results"] is None
        assert p1["reasoning_steps"] == []
        assert p2["last_recall_results"] is None
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

    def test_preserves_location_and_summaries(self) -> None:
        loc = {"lat": 13.7, "lng": 100.5}
        p = build_turn_payload(
            message="hi",
            user_id="u1",
            taste_profile_summary="likes ramen",
            memory_summary="vegetarian",
            location=loc,
        )
        assert p["taste_profile_summary"] == "likes ramen"
        assert p["memory_summary"] == "vegetarian"
        assert p["location"] == loc
        assert p["user_id"] == "u1"

    def test_location_defaults_to_none(self) -> None:
        p = build_turn_payload(
            message="hi",
            user_id="u1",
            taste_profile_summary="",
            memory_summary="",
        )
        assert p["location"] is None

    def test_location_label_threaded_when_provided(self) -> None:
        p = build_turn_payload(
            message="hi",
            user_id="u1",
            taste_profile_summary="",
            memory_summary="",
            location={"lat": 52.12, "lng": 11.62},
            location_label="Magdeburg, Germany",
        )
        assert p["location_label"] == "Magdeburg, Germany"

    def test_location_label_defaults_to_none(self) -> None:
        p = build_turn_payload(
            message="hi",
            user_id="u1",
            taste_profile_summary="",
            memory_summary="",
        )
        assert p["location_label"] is None
