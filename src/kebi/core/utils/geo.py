"""Geographic utilities — distance calculations and location operations."""

import math

# Metres per degree of latitude (WGS-84 mean). Longitude scales by cos(lat).
_M_PER_DEG_LAT = 111_320.0


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """
    Calculate the great-circle distance between two points on Earth.

    Uses the Haversine formula. Returns distance in metres.

    Args:
        lat1: Latitude of first point (degrees)
        lng1: Longitude of first point (degrees)
        lat2: Latitude of second point (degrees)
        lng2: Longitude of second point (degrees)

    Returns:
        Distance in metres

    """
    # Earth's radius in metres
    R = 6_371_000

    # Convert to radians
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)

    # Haversine formula
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.asin(math.sqrt(a))

    return R * c


def bounding_box(
    lat: float, lng: float, radius_m: float
) -> tuple[float, float, float, float]:
    """Axis-aligned box that circumscribes the circle of `radius_m` at (lat, lng).

    Returns `(low_lat, low_lng, high_lat, high_lng)`. A degree of latitude is
    ~111.32 km everywhere; a degree of longitude shrinks by cos(latitude). The
    box's corners reach ~1.41× the radius, so it fully contains the circle —
    used as a *hard* `locationRestriction.rectangle` for the place provider,
    which (for text search) accepts a rectangle but not a circle.

    `cos(lat)` is floored to a small positive value so the longitude span stays
    finite at the poles rather than dividing by zero.
    """
    dlat = radius_m / _M_PER_DEG_LAT
    cos_lat = max(math.cos(math.radians(lat)), 0.01)
    dlng = radius_m / (_M_PER_DEG_LAT * cos_lat)
    return (lat - dlat, lng - dlng, lat + dlat, lng + dlng)
