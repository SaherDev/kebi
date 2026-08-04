"""Tests for the WorkingLocation and LocationResolution models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kebi.core.agent.location import (
    CorridorPath,
    CorridorTarget,
    LocationResolution,
    WorkingLocation,
    density_class,
    resolve_radius,
)
from kebi.core.config import MovementConfig


def test_working_location_requires_core_fields() -> None:
    with pytest.raises(ValidationError):
        WorkingLocation(city="Tokyo", lat=35.6, lng=139.7)  # type: ignore[call-arg]


def test_working_location_neighborhood_is_optional() -> None:
    wl = WorkingLocation(country="Japan", city="Tokyo", lat=35.6, lng=139.7)
    assert wl.neighborhood is None


def test_working_location_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        WorkingLocation(
            country="Japan",
            city="Tokyo",
            lat=35.6,
            lng=139.7,
            region="Kanto",  # type: ignore[call-arg]
        )


def test_location_resolution_accepts_partial_with_flags() -> None:
    r = LocationResolution(
        source="explicit_query",
        city="Cambridge",
        is_ambiguous=True,
        needs_clarification=True,
        clarification_reason="UK or Massachusetts?",
    )
    assert r.country is None
    assert r.is_ambiguous is True


def test_location_resolution_requires_source() -> None:
    with pytest.raises(ValidationError):
        LocationResolution()  # type: ignore[call-arg]


def test_location_resolution_defaults_to_area_city_scope() -> None:
    r = LocationResolution(source="carried")
    assert r.scope_tier == "city"
    assert r.scope_shape == "area"
    assert r.effective_mode is None
    assert r.corridor_destinations == []


def test_working_location_scope_fields_default_to_neutral() -> None:
    """A bare WorkingLocation (built outside the resolve node) carries
    neutral scope defaults — the node overwrites them every turn."""
    wl = WorkingLocation(country="Japan", city="Tokyo", lat=35.6, lng=139.7)
    assert wl.scope_shape == "area"
    assert wl.scope_tier == "city"
    assert wl.search_radius_m == 0.0
    assert wl.corridor is None


# --- resolve_radius (ADR-084) ----------------------------------------------

_CFG = MovementConfig()  # walkable=1000 nbhd=2500 city=7000 metro=45000
#         walking×1.0 cycling×1.5 transit×2.0 ride×2.2 drive×2.6
#         density: dense×0.7 medium×1.0 sparse×1.6


def test_resolve_radius_tier_times_mode_multiplier() -> None:
    # walkable base 1000 × walking 1.0 × medium 1.0
    assert resolve_radius("walking", "walkable", "normal", "medium", _CFG) == 1000.0
    # city base 7000 × driving 2.6 × medium 1.0
    assert resolve_radius("driving", "city", "normal", "medium", _CFG) == 7000.0 * 2.6
    # metro base 45000 × transit 2.0 × medium 1.0
    assert resolve_radius("transit", "metro", "normal", "medium", _CFG) == 45000.0 * 2.0


def test_resolve_radius_reach_shifts_the_tier() -> None:
    # `far` shifts city → metro before the lookup.
    assert resolve_radius("walking", "city", "far", "medium", _CFG) == 45000.0 * 1.0
    # `compact` shifts neighborhood → walkable.
    assert (
        resolve_radius("walking", "neighborhood", "compact", "medium", _CFG) == 1000.0
    )


def test_resolve_radius_reach_shift_clamps_at_the_ends() -> None:
    # `compact` cannot go below walkable.
    assert resolve_radius("walking", "walkable", "compact", "medium", _CFG) == 1000.0
    # `far` cannot go above metro.
    assert resolve_radius("walking", "metro", "far", "medium", _CFG) == 45000.0


def test_resolve_radius_far_reach_on_constrained_walking_profile_is_sane() -> None:
    """A walking-only user with `reach: far` asking about a `walkable` turn
    should land at the neighborhood base — wider than a plain walkable turn,
    still scaled by the walking multiplier, never absurd."""
    walkable = resolve_radius("walking", "walkable", "normal", "medium", _CFG)
    far = resolve_radius("walking", "walkable", "far", "medium", _CFG)
    assert far > walkable
    assert far == 2500.0  # neighborhood base × walking 1.0 × medium 1.0


def test_resolve_radius_density_scales_the_same_tier() -> None:
    """The same walkable turn reaches further in a sparse area than a dense
    one — the fix for the density-blind radius."""
    dense = resolve_radius("walking", "walkable", "normal", "dense", _CFG)
    medium = resolve_radius("walking", "walkable", "normal", "medium", _CFG)
    sparse = resolve_radius("walking", "walkable", "normal", "sparse", _CFG)
    assert dense < medium < sparse
    assert dense == 1000.0 * 0.7
    assert sparse == 1000.0 * 1.6


def test_resolve_radius_unknown_mode_tier_density_degrade_safely() -> None:
    # Unknown mode → multiplier 1.0; unknown tier → city; unknown density → 1.0.
    assert resolve_radius("teleport", "city", "normal", "medium", _CFG) == 7000.0
    assert resolve_radius("walking", "moon", "normal", "medium", _CFG) == 7000.0
    assert resolve_radius("walking", "city", "normal", "void", _CFG) == 7000.0


# --- density_class (ADR-084) -----------------------------------------------


def test_density_class_maps_geocoder_place_types() -> None:
    assert density_class("city") == "dense"
    assert density_class("suburb") == "dense"
    assert density_class("city_district") == "dense"
    assert density_class("town") == "medium"
    assert density_class("village") == "sparse"
    assert density_class("hamlet") == "sparse"


def test_density_class_degrades_to_medium() -> None:
    # Missing or unrecognised place types are neutral.
    assert density_class(None) == "medium"
    assert density_class("") == "medium"
    assert density_class("road") == "medium"


# --- CorridorPath / legacy checkpoint shape (ADR-136) ----------------------


def test_corridor_path_exposes_the_polyline_origin_first() -> None:
    path = CorridorPath(
        stops=[
            CorridorTarget(name="Hue", lat=16.46, lng=107.59),
            CorridorTarget(name="Hoi An", lat=15.88, lng=108.33),
        ]
    )
    assert path.points(16.05, 108.20) == [
        (16.05, 108.20),
        (16.46, 107.59),
        (15.88, 108.33),
    ]
    assert path.destination.name == "Hoi An"


def test_checkpointed_single_destination_still_loads() -> None:
    """A conversation in flight when the multi-stop shape shipped carries the
    old single `CorridorTarget` dict. `WorkingLocation` forbids extra keys, so
    without the coercion the turn would fail validation on resume."""
    wl = WorkingLocation.model_validate(
        {
            "country": "Vietnam",
            "city": "Da Nang",
            "lat": 16.05,
            "lng": 108.20,
            "scope_shape": "corridor",
            "corridor": {"name": "Hue", "lat": 16.46, "lng": 107.59},
        }
    )
    assert wl.corridor is not None
    assert [s.name for s in wl.corridor.stops] == ["Hue"]


def test_new_path_shape_round_trips() -> None:
    wl = WorkingLocation(
        country="Vietnam",
        city="Da Nang",
        lat=16.05,
        lng=108.20,
        scope_shape="corridor",
        corridor=CorridorPath(
            stops=[CorridorTarget(name="Hue", lat=16.46, lng=107.59)]
        ),
    )
    assert WorkingLocation.model_validate(wl.model_dump()) == wl
