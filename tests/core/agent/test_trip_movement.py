"""Tests for trip-scoped movement and the unset-vs-set flag (ADR-155).

Two failures motivate this file. A settings row seeded with a neutral
capability arrived on the wire indistinguishable from a deliberate answer,
so kebi asserted walking range to people who drive and never asked. And
transport was modelled as a lifelong trait when for a travelling user it is
a fact about the current stay.
"""

from __future__ import annotations

from typing import Any, cast

from kebi.core.agent.graph import (
    _mobility_profile,
    _next_trip_movement,
    _render_movement_context,
)
from kebi.core.agent.location import LocationResolution, WorkingLocation
from kebi.core.agent.state import TRIP_MOVEMENT_INHERIT, merge_trip_movement


def _wl(country: str = "Indonesia", city: str = "Badung") -> WorkingLocation:
    return WorkingLocation(
        country=country,
        city=city,
        neighborhood="Canggu",
        lat=-8.65,
        lng=115.13,
    )


def _res(**kw: Any) -> LocationResolution:
    base: dict[str, Any] = {"source": "user_actual"}
    base.update(kw)
    return LocationResolution(**base)


# --- unset vs set ----------------------------------------------------------


def test_a_seeded_default_is_not_treated_as_the_user_s_answer() -> None:
    """The production bug in one assertion.

    Every user's settings row is seeded with a neutral capability and sent on
    every turn. Read as an answer, it silently disables the ask-don't-assert
    path, which is the only thing that would have surfaced the gap.
    """
    modes, _reach, is_fallback = _mobility_profile(
        {"available_modes": ["walking", "transit"], "source": "default"}
    )

    assert is_fallback is True
    # And its narrow modes are ignored (ADR-156): honouring a seeded guess
    # would reinstate the silent capping this exists to end.
    assert modes[0] == "rideshare"


def test_a_chosen_profile_is_trusted() -> None:
    _modes, _reach, is_fallback = _mobility_profile(
        {"available_modes": ["driving"], "source": "user"}
    )

    assert is_fallback is False


def test_a_missing_profile_is_still_unresolved() -> None:
    modes, _reach, is_fallback = _mobility_profile(None)

    assert is_fallback is True
    assert modes  # the config fallback keeps the radius math working


def test_a_profile_with_no_source_reads_as_unchosen() -> None:
    """Absent means unchosen, because today nobody can have chosen.

    No client has a screen for this yet, so a request that omits the flag is
    describing a guess. The safe reading is the honest one.
    """
    modes, _reach, is_fallback = _mobility_profile(
        {"available_modes": ["walking", "transit"]}
    )

    assert is_fallback is True
    assert modes[0] == "rideshare"


# --- trip modes outrank everything ----------------------------------------


def test_stated_trip_modes_beat_a_chosen_profile() -> None:
    """First-hand and current beats saved and general.

    A user who says "we rented a scooter" has told you something their
    settings row cannot know, even when that row was filled in deliberately.
    """
    modes, _reach, is_fallback = _mobility_profile(
        {"available_modes": ["walking", "transit"], "source": "user"},
        {"available_modes": ["motorbike"]},
    )

    assert modes == ["motorbike"]
    assert is_fallback is False


def test_stated_trip_modes_resolve_the_unknown() -> None:
    """Someone who answered the question is no longer an unknown."""
    _modes, _reach, is_fallback = _mobility_profile(
        None, {"available_modes": ["cycling"]}
    )

    assert is_fallback is False


# --- the trip lifetime -----------------------------------------------------


def test_a_stated_mode_is_recorded() -> None:
    update = _next_trip_movement(_res(stated_modes=["motorbike"]), _wl(), None)

    assert update == {"available_modes": ["motorbike"]}


def test_a_new_statement_replaces_the_old_one() -> None:
    """ "The scooter went back, we're walking" is a correction, not an addition."""
    update = _next_trip_movement(
        _res(stated_modes=["walking"]), _wl(), {"country": "Indonesia"}
    )

    assert update == {"available_modes": ["walking"]}


def test_moving_within_the_country_keeps_the_trip_modes() -> None:
    """Canggu to Ubud is the same trip, and the scooter came along."""
    update = _next_trip_movement(_res(), _wl(city="Ubud"), {"country": "Indonesia"})

    assert update == TRIP_MOVEMENT_INHERIT


def test_changing_country_clears_the_trip_modes() -> None:
    """A new country is a new trip; last trip's rental is not this one's."""
    update = _next_trip_movement(
        _res(), _wl(country="Singapore", city="Singapore"), {"country": "Indonesia"}
    )

    assert update is None


def test_country_comparison_ignores_case_and_padding() -> None:
    """A geocoder casing difference must not read as crossing a border."""
    update = _next_trip_movement(
        _res(), _wl(country="indonesia"), {"country": " Indonesia "}
    )

    assert update == TRIP_MOVEMENT_INHERIT


def test_a_first_turn_with_no_carried_location_keeps_whatever_is_there() -> None:
    """No previous country is not a country change."""
    assert _next_trip_movement(_res(), _wl(), None) == TRIP_MOVEMENT_INHERIT


# --- the reducer -----------------------------------------------------------


def test_the_sentinel_carries_the_prior_value() -> None:
    assert merge_trip_movement(
        {"available_modes": ["motorbike"]}, TRIP_MOVEMENT_INHERIT
    ) == {"available_modes": ["motorbike"]}


def test_a_dict_replaces_and_none_clears() -> None:
    prior = {"available_modes": ["motorbike"]}

    assert merge_trip_movement(prior, {"available_modes": ["walking"]}) == {
        "available_modes": ["walking"]
    }
    assert merge_trip_movement(prior, None) is None


# --- what the agent is told ------------------------------------------------


def _state(**kw: Any) -> Any:
    base: dict[str, Any] = {
        "working_location": {
            "effective_mode": "motorbike",
            "scope_tier": "city",
            "scope_shape": "area",
            "search_radius_m": 16800.0,
        },
        "movement_profile": None,
        "trip_movement": None,
    }
    base.update(kw)
    return cast(Any, base)


def test_the_prompt_says_a_stated_mode_is_settled() -> None:
    rendered = _render_movement_context(
        _state(trip_movement={"available_modes": ["motorbike"]})
    )

    assert "motorbike" in rendered
    assert "do not ask again" in rendered


def test_the_prompt_flags_a_guess_as_a_guess() -> None:
    """The wording matters: "no profile" reads as a bug, "not their answer"
    reads as a question worth asking."""
    rendered = _render_movement_context(
        _state(movement_profile={"available_modes": ["walking"], "source": "default"})
    )

    assert "That is a guess" in rendered
    assert "owe them the distance out loud" in rendered


def test_a_chosen_profile_draws_no_caveat() -> None:
    rendered = _render_movement_context(
        _state(movement_profile={"available_modes": ["driving"], "source": "user"})
    )

    assert "That is a guess" not in rendered


def test_the_sentinel_never_survives_onto_state() -> None:
    """The first-turn trap.

    On a brand-new thread LangGraph stores the seeded sentinel as-is, so a
    naive "keep" hands the string straight back. The tools validate injected
    state against the annotation, so a lingering sentinel fails every tool
    call on the first turn of every conversation.
    """
    assert merge_trip_movement(TRIP_MOVEMENT_INHERIT, TRIP_MOVEMENT_INHERIT) is None
    assert merge_trip_movement(None, TRIP_MOVEMENT_INHERIT) is None


def test_an_echoed_mode_does_not_survive_a_border() -> None:
    """The bug live testing caught.

    The resolver reads conversation history, so on the turn where someone says
    "we're in Singapore now" it restates the scooter they mentioned in Bali.
    Arriving before the country check, that restatement kept overriding the
    clear, and the rental followed its owner across the border.
    """
    update = _next_trip_movement(
        _res(stated_modes=["motorbike"]),
        _wl(country="Singapore", city="Singapore"),
        {"country": "Indonesia"},
        {"available_modes": ["motorbike"]},
    )

    assert update is None


def test_a_genuinely_new_statement_survives_a_border() -> None:
    """Picking up a car on arrival is a statement, not an echo.

    The two are told apart by whether the modes differ from what the trip
    already held, which is what makes the rule deterministic rather than a
    guess about the model's intent.
    """
    update = _next_trip_movement(
        _res(stated_modes=["driving"]),
        _wl(country="Singapore", city="Singapore"),
        {"country": "Indonesia"},
        {"available_modes": ["motorbike"]},
    )

    assert update == {"available_modes": ["driving"]}


def test_an_echo_within_the_same_country_is_harmless() -> None:
    """Restating the scooter in Bali changes nothing, so it need not be caught."""
    update = _next_trip_movement(
        _res(stated_modes=["motorbike"]),
        _wl(city="Ubud"),
        {"country": "Indonesia"},
        {"available_modes": ["motorbike"]},
    )

    assert update == {"available_modes": ["motorbike"]}


# --- guessing wide when ignorant (ADR-156) ---------------------------------


def test_an_unknown_user_is_guessed_wide_not_narrow() -> None:
    """Narrow-when-ignorant fails invisibly, which is the worse failure.

    A driver capped at walking range never learns the places past it were
    dropped, so nothing corrects it. Guessing wide fails out loud instead: a
    pick turns out to be a drive, the user says so, the turn recovers.
    """
    modes, _reach, is_fallback = _mobility_profile(None)

    assert is_fallback is True
    # Rideshare assumes no licence, no vehicle, no fitness, and reaches what a
    # car reaches — the one mode almost anyone has almost anywhere.
    assert modes[0] == "rideshare"


def test_the_guess_is_never_passed_off_as_knowledge() -> None:
    """Widening only works paired with saying so; silent widening is the same
    class of bug as silent narrowing, just in the other direction."""
    rendered = _render_movement_context(
        _state(movement_profile={"available_modes": ["walking"], "source": "default"})
    )

    assert "That is a guess" in rendered
    assert "owe them the distance out loud" in rendered


# --- the widening floor (ADR-156) ------------------------------------------


async def _scope(resolution: Any, movement: dict[str, Any] | None) -> Any:
    from kebi.core.agent.graph import _resolve_search_scope

    return await _resolve_search_scope(_wl(), resolution, movement, None)


async def test_an_inferred_narrow_mode_is_widened_when_capability_is_unknown() -> None:
    """The resolver keeps picking walking when it is unsure, which is right
    about an unfamiliar city and wrong about an unknown user. Told not to in
    the prompt, it did it anyway, so the floor is enforced in code."""
    working = await _scope(
        _res(effective_mode="walking"),
        {"available_modes": ["walking", "transit"]},  # unchosen: a guess
    )

    assert working is not None
    assert working.effective_mode == "rideshare"


async def test_a_mode_the_user_actually_said_is_never_widened() -> None:
    """ "Walking distance" is not a guess, so the floor must not touch it."""
    working = await _scope(
        _res(effective_mode="walking", mode_is_explicit=True),
        {"available_modes": ["walking", "transit"]},
    )

    assert working is not None
    assert working.effective_mode == "walking"


async def test_a_chosen_capability_is_never_widened() -> None:
    """Someone who said they only walk is answered for walking, full stop."""
    working = await _scope(
        _res(effective_mode="walking"),
        {"available_modes": ["walking"], "source": "user"},
    )

    assert working is not None
    assert working.effective_mode == "walking"


async def test_a_wider_inferred_mode_is_left_alone() -> None:
    """The rule is a floor, not a target — it never narrows anything."""
    working = await _scope(
        _res(effective_mode="driving"), {"available_modes": ["walking", "transit"]}
    )

    assert working is not None
    assert working.effective_mode == "driving"


async def test_a_walkable_tier_turn_is_not_widened() -> None:
    """ "Anywhere round the corner?" is already a statement that this one is on
    foot, so widening it would answer a different question than the one asked.
    """
    working = await _scope(
        _res(effective_mode="walking", scope_tier="walkable"),
        {"available_modes": ["walking", "transit"]},
    )

    assert working is not None
    assert working.effective_mode == "walking"
