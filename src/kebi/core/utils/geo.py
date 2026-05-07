"""Geographic utilities — distance calculations and location operations."""

import math


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
