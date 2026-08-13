"""Areas — the geo entity's own surface (ADR-153).

A venue has a catalog row; an area, until now, existed only as a geo key
threaded through claims and chat links. This package gives that key a row
and a screen: a global profile generated on first open (`profile_service`),
the id codec that makes the slash-path key routable (`keys`), and the
read-side composition behind `GET /v1/areas/{id}` (`screen_service`).
"""

from .keys import decode_area_id, display_from_slug, encode_area_id, parent_keys
from .models import AreaChip, AreaProfile, NotableSubArea
from .profile_service import AreaProfileService
from .screen_service import AreaScreenService

__all__ = [
    "AreaChip",
    "AreaProfile",
    "AreaProfileService",
    "AreaScreenService",
    "NotableSubArea",
    "decode_area_id",
    "display_from_slug",
    "encode_area_id",
    "parent_keys",
]
