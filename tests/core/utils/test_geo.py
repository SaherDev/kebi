"""Tests for geo utilities — bounding_box and route-path geometry."""

from __future__ import annotations

import math

import pytest

from kebi.core.utils.geo import (
    bounding_box,
    distance_to_path_m,
    enclosing_circle,
    haversine_m,
    leg_lengths_m,
    path_length_m,
    path_progress,
    sample_path_waypoints,
)

# Shinjuku-side anchor used throughout the utility-radius work.
_LAT, _LNG = 35.6762, 139.6503


def test_box_contains_a_near_point() -> None:
    lo_lat, lo_lng, hi_lat, hi_lng = bounding_box(_LAT, _LNG, 1000.0)
    # ~500 m east stays well inside a 1 km box.
    near_lat, near_lng = _LAT, _LNG + 0.005
    assert haversine_m(_LAT, _LNG, near_lat, near_lng) < 1000
    assert lo_lat <= near_lat <= hi_lat
    assert lo_lng <= near_lng <= hi_lng


def test_box_excludes_the_far_marunouchi_point() -> None:
    """The 10 km branch that motivated lever 3 must fall outside a 1 km box."""
    lo_lat, lo_lng, hi_lat, hi_lng = bounding_box(_LAT, _LNG, 1000.0)
    far_lat, far_lng = 35.6773, 139.7648  # Marunouchi, ~10 km east
    assert haversine_m(_LAT, _LNG, far_lat, far_lng) > 9000
    assert far_lng > hi_lng  # outside the eastern edge


def test_box_widens_with_radius() -> None:
    narrow = bounding_box(_LAT, _LNG, 1000.0)
    wide = bounding_box(_LAT, _LNG, 5000.0)
    assert wide[0] < narrow[0] and wide[1] < narrow[1]  # lows further out
    assert wide[2] > narrow[2] and wide[3] > narrow[3]  # highs further out


def test_longitude_span_wider_than_latitude_span_away_from_equator() -> None:
    """A degree of longitude is shorter at latitude, so the lng half-span
    (in degrees) for the same metres is larger than the lat half-span."""
    lo_lat, lo_lng, hi_lat, hi_lng = bounding_box(_LAT, _LNG, 1000.0)
    lat_span = hi_lat - lo_lat
    lng_span = hi_lng - lo_lng
    assert lng_span > lat_span


# ---------------------------------------------------------------------------
# Path geometry (ADR-136) — a route is an ordered polyline, origin first.
# Coordinates below are the real Da Nang → Hue corridor (~80 km) and the
# Hanoi → Hue → Hoi An chain, so the assertions read against real distances.
# ---------------------------------------------------------------------------

_DA_NANG = (16.0544, 108.2022)
_HUE = (16.4637, 107.5909)
_HOI_AN = (15.8801, 108.3380)
_HANOI = (21.0278, 105.8342)
_LANG_CO = (16.2333, 108.0667)  # roughly midway Da Nang → Hue


class TestPathLength:
    def test_leg_lengths_match_point_distances(self) -> None:
        path = [_DA_NANG, _HUE, _HOI_AN]
        legs = leg_lengths_m(path)
        assert len(legs) == 2
        assert legs[0] == pytest.approx(haversine_m(*_DA_NANG, *_HUE), rel=1e-9)
        assert path_length_m(path) == pytest.approx(sum(legs), rel=1e-9)

    def test_degenerate_paths_have_no_length(self) -> None:
        assert path_length_m([]) == 0.0
        assert path_length_m([_DA_NANG]) == 0.0


class TestDistanceToPath:
    def test_point_on_the_route_is_within_the_half_width(self) -> None:
        """Lang Co is a real stop on the Da Nang → Hue drive, but the coast
        bends east while the straight line cuts inland — so it sits ~8 km
        off. That gap is why the default half-width is 15 km: a v1
        straight-line corridor has to tolerate the road's real shape."""
        assert distance_to_path_m([_DA_NANG, _HUE], *_LANG_CO) < 15_000

    def test_point_far_off_the_route_is_far(self) -> None:
        """Hoi An sits south of Da Nang — the wrong way for a Hue trip."""
        assert distance_to_path_m([_DA_NANG, _HUE], *_HOI_AN) > 20_000

    def test_second_leg_pulls_a_point_back_onto_the_path(self) -> None:
        """Off the first leg but on the second still counts as on-route —
        the distance is to the polyline, not to any single segment."""
        chain = [_DA_NANG, _HUE, _HOI_AN]
        assert distance_to_path_m(chain, *_HOI_AN) == pytest.approx(0.0, abs=1.0)

    def test_single_point_path_degrades_to_point_distance(self) -> None:
        assert distance_to_path_m([_DA_NANG], *_HUE) == pytest.approx(
            haversine_m(*_DA_NANG, *_HUE), rel=1e-9
        )

    def test_empty_path_is_infinitely_far(self) -> None:
        assert distance_to_path_m([], *_HUE) == math.inf


class TestPathProgress:
    def test_endpoints_anchor_zero_and_one(self) -> None:
        path = [_DA_NANG, _HUE]
        assert path_progress(path, *_DA_NANG) == pytest.approx(0.0, abs=1e-3)
        assert path_progress(path, *_HUE) == pytest.approx(1.0, abs=1e-3)

    def test_midpoint_sits_between(self) -> None:
        p = path_progress([_DA_NANG, _HUE], *_LANG_CO)
        assert 0.2 < p < 0.8

    def test_ordering_by_progress_runs_origin_to_destination(self) -> None:
        path = [_DA_NANG, _HUE]
        points = [_HUE, _LANG_CO, _DA_NANG]
        ordered = sorted(points, key=lambda p: path_progress(path, *p))
        assert ordered == [_DA_NANG, _LANG_CO, _HUE]

    def test_progress_is_cumulative_across_legs(self) -> None:
        """A stop on the second leg reads as further along than one on the
        first — otherwise a chain would interleave its legs."""
        chain = [_HANOI, _HUE, _HOI_AN]
        assert path_progress(chain, *_HUE) < path_progress(chain, *_HOI_AN)
        assert path_progress(chain, *_HOI_AN) == pytest.approx(1.0, abs=1e-3)

    def test_beyond_the_end_clamps_rather_than_overshooting(self) -> None:
        far_north = (18.0, 107.0)  # well past Hue
        assert path_progress([_DA_NANG, _HUE], *far_north) <= 1.0

    def test_zero_length_path_ties_everything_at_zero(self) -> None:
        assert path_progress([_DA_NANG, _DA_NANG], *_HUE) == 0.0
        assert path_progress([_DA_NANG], *_HUE) == 0.0


class TestSampleWaypoints:
    def test_named_stops_are_always_sampled(self) -> None:
        chain = [_HANOI, _HUE, _HOI_AN]
        points = sample_path_waypoints(
            chain, spacing_m=25_000, max_count=3, skip_legs=frozenset()
        )
        for stop in chain:
            assert stop in points

    def test_interior_points_fall_between_the_endpoints(self) -> None:
        points = sample_path_waypoints([_DA_NANG, _HUE], spacing_m=25_000, max_count=5)
        assert points[0] == _DA_NANG
        assert points[-1] == _HUE
        assert len(points) > 2
        progress = [path_progress([_DA_NANG, _HUE], *p) for p in points]
        assert progress == sorted(progress)

    def test_budget_caps_the_fan_out(self) -> None:
        """A 1,600 km route asks for many points and gets the cap."""
        points = sample_path_waypoints([_HANOI, _HOI_AN], spacing_m=25_000, max_count=5)
        assert len(points) == 5

    def test_skipped_leg_gets_no_interior_points(self) -> None:
        """The length gate: an over-long leg contributes only its endpoints."""
        chain = [_HANOI, _HUE, _HOI_AN]
        points = sample_path_waypoints(
            chain, spacing_m=25_000, max_count=5, skip_legs=frozenset({0})
        )
        first_leg_interior = [
            p
            for p in points
            if p not in chain and path_progress(chain, *p) < path_progress(chain, *_HUE)
        ]
        assert first_leg_interior == []

    def test_all_legs_skipped_leaves_only_the_stops(self) -> None:
        chain = [_HANOI, _HUE, _HOI_AN]
        points = sample_path_waypoints(
            chain, spacing_m=25_000, max_count=5, skip_legs=frozenset({0, 1})
        )
        assert points == chain

    def test_longer_leg_takes_the_larger_share_of_a_tight_budget(self) -> None:
        chain = [_HANOI, _HUE, _HOI_AN]  # ~600 km then ~80 km
        points = sample_path_waypoints(chain, spacing_m=25_000, max_count=5)
        cut = path_progress(chain, *_HUE)
        interior = [p for p in points if p not in chain]
        on_long_leg = [p for p in interior if path_progress(chain, *p) < cut]
        assert len(on_long_leg) > len(interior) - len(on_long_leg)

    def test_short_path_is_returned_as_is(self) -> None:
        assert sample_path_waypoints([_DA_NANG], spacing_m=25_000, max_count=5) == [
            _DA_NANG
        ]

    def test_zero_length_leg_does_not_divide_by_zero(self) -> None:
        points = sample_path_waypoints(
            [_DA_NANG, _DA_NANG], spacing_m=25_000, max_count=5
        )
        assert points == [_DA_NANG, _DA_NANG]


class TestEnclosingCircle:
    def test_circle_reaches_every_path_point(self) -> None:
        chain = [_HANOI, _HUE, _HOI_AN]
        lat, lng, radius = enclosing_circle(chain, pad_m=0.0)
        for point in chain:
            assert haversine_m(lat, lng, *point) <= radius + 1.0

    def test_pad_widens_the_circle(self) -> None:
        _, _, tight = enclosing_circle([_DA_NANG, _HUE], pad_m=0.0)
        _, _, padded = enclosing_circle([_DA_NANG, _HUE], pad_m=15_000.0)
        assert padded == pytest.approx(tight + 15_000.0, rel=1e-9)

    def test_empty_path_is_a_pad_sized_circle_at_the_origin(self) -> None:
        assert enclosing_circle([], pad_m=500.0) == (0.0, 0.0, 500.0)
