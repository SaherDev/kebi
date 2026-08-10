"""Tests for _render_user_profile_context — the {user_profile_context} slot.

The slot renders the "about me" block the user filled in (ADR-154). Two of
its three fields are free text the user typed, so the assertions here are as
much about containment — the block stays inside a `trust="low"` wrapper, the
weighting instructions stay outside it — as about the copy itself.
"""

from __future__ import annotations

from typing import Any, cast

from kebi.core.agent.graph import _render_system_prompt, _render_user_profile_context


def _state(user_profile: dict[str, Any] | None = None) -> Any:
    return cast(
        Any,
        {
            "working_location": None,
            "location_clarification": None,
            "movement_profile": None,
            "trip_movement": None,
            "user_profile": user_profile,
            "taste_profile_summary": "",
            "memory_summary": "",
        },
    )


def test_absent_profile_says_so_rather_than_inventing() -> None:
    """No profile must read as "you know nothing", not as an empty block.

    A silent gap is the failure mode that ends with the agent addressing a
    brand-new user by a name it made up.
    """
    rendered = _render_user_profile_context(_state(None))

    assert "has not filled in an about-me profile" in rendered
    assert "do not invent" in rendered


def test_profile_present_but_all_fields_empty_reads_as_absent() -> None:
    """A dict of Nones is a user who filled in nothing, not a partial profile."""
    rendered = _render_user_profile_context(
        _state({"call_me": None, "home_country": None, "about": None})
    )

    assert "has not filled in an about-me profile" in rendered


def test_fields_render_and_stay_inside_the_untrusted_wrapper() -> None:
    """User-typed content is data; the instructions about it are not.

    The wrapper boundary is the whole point: everything the user wrote sits
    between the tags, and every rule about how to weigh it sits after the
    closing tag where the user's text cannot reach it.
    """
    rendered = _render_user_profile_context(
        _state(
            {
                "call_me": "Saher",
                "home_country": "AE",
                "about": "I'd rather eat where locals eat. I don't drink.",
            }
        )
    )

    body = rendered.split('<user_profile trust="low">')[1].split("</user_profile>")[0]
    assert "Saher" in body
    assert "AE" in body
    assert "I'd rather eat where locals eat" in body

    after = rendered.split("</user_profile>")[1]
    assert "behavior wins" in after
    assert "does not decay" in after


def test_partial_profile_omits_the_fields_that_are_absent() -> None:
    """A user who gave only a name gets only a name — no empty labels."""
    rendered = _render_user_profile_context(_state({"call_me": "Saher"}))

    assert "Goes by: Saher" in rendered
    assert "Home country" not in rendered
    assert "In their own words" not in rendered


def test_home_country_pulls_in_the_entry_rule_lookup_instruction() -> None:
    """A passport country is the half of an entry question that must be looked up.

    Entry rules read as stable and are not, so the instruction to search
    every time rides along with the country rather than living in the static
    prompt where it would fire for users who never gave one.
    """
    rendered = _render_user_profile_context(_state({"home_country": "AE"}))

    assert "web_search" in rendered
    assert "Never answer an entry question from memory" in rendered


def test_no_home_country_means_no_entry_rule_instruction() -> None:
    rendered = _render_user_profile_context(_state({"call_me": "Saher"}))

    assert "web_search" not in rendered


def test_braces_in_about_cannot_hijack_the_parent_format() -> None:
    """`about` is substituted into a template that is `.format()`-ed.

    Braces the user typed are escaped before substitution, so a stray
    `{oops}` can neither raise a KeyError on the parent template nor be
    mistaken for a slot by a later `.format(...)` pass. The escaped form is
    what reaches the model — the same treatment the taste and memory
    summaries already get.
    """
    static_head, dynamic_tail = _render_system_prompt(
        _state({"about": "I like {taste_profile_summary} and {oops}"})
    )

    assert "{{oops}}" in dynamic_tail
    assert "I like" in dynamic_tail
    assert static_head


def test_slot_is_substituted_in_the_rendered_prompt() -> None:
    """The registered slot must actually resolve — no raw `{...}` reaches the model."""
    _static_head, dynamic_tail = _render_system_prompt(_state({"call_me": "Saher"}))

    assert "{user_profile_context}" not in dynamic_tail
    assert "Goes by: Saher" in dynamic_tail


def test_a_name_comes_with_guidance_on_using_it() -> None:
    """A bare `Goes by: X` gets ignored.

    The rule itself lives in the prompt's Voice section, which outranks the
    register-mirroring that otherwise suppresses a greeting; the slot only
    points at it, so the two cannot drift into contradicting each other.
    """
    rendered = _render_user_profile_context(_state({"call_me": "Saher"}))

    assert "the name to address them by" in rendered
    assert "Voice section" in rendered


def test_no_name_means_no_naming_instruction() -> None:
    """Nothing should tell the agent how to address someone it cannot name."""
    rendered = _render_user_profile_context(_state({"home_country": "AE"}))

    assert "the name to address them by" not in rendered
