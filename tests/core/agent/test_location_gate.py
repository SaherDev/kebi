"""Tests for _needs_location_resolution — the start-of-turn location gate."""

from __future__ import annotations

from typing import Any, cast

import pytest
from langchain_core.messages import HumanMessage

from kebi.core.agent.graph import _needs_location_resolution


def _state(message: str) -> Any:
    return cast(Any, {"messages": [HumanMessage(content=message)]})


@pytest.mark.parametrize(
    "message",
    [
        "hi",
        "hey there",
        "what did i save last week",
        "thanks so much",
        "can you help me",
    ],
)
def test_location_free_turns_skip(message: str) -> None:
    assert _needs_location_resolution(_state(message)) == "skip"


@pytest.mark.parametrize(
    "message",
    [
        "ramen near me",
        "what about Tokyo",
        "anything around here",
        "good cafes in the area",
        "what about tel aviv",
        "where should i go",
        "I'm headed to Lyon tomorrow",
        # Lowercased place names + travel-intent phrasings — the
        # proper-noun heuristic doesn't catch these on its own; the
        # keyword/phrase fallback has to.
        "I'm in old town chiang mai next week — what are the famous food spots?",
        "i'll be visiting chiang rai next month",
        "going to hua hin for the weekend, any tips?",
        "on vacation in koh lipe, where should i eat?",
    ],
)
def test_location_relevant_turns_resolve(message: str) -> None:
    assert _needs_location_resolution(_state(message)) == "resolve"


def test_empty_messages_skip() -> None:
    assert _needs_location_resolution(cast(Any, {"messages": []})) == "skip"
