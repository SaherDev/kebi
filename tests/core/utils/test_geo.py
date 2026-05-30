"""Tests for geo utilities — bounding_box."""

from __future__ import annotations

from kebi.core.utils.geo import bounding_box, haversine_m

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
