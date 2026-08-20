"""Areas — the geo entity's own surface (ADR-153).

A venue has a catalog row; an area, until now, existed only as a geo key
threaded through claims and chat links. This package gives that key a row
and a screen: a global profile generated on first open (`profile_service`),
the id codec that makes the slash-path key routable (`keys`), and the
read-side composition behind `GET /v1/areas/{id}` (`screen_service`).

The two service classes are re-exported **lazily**. The places catalog now
derives a place's area key on write (ADR-165), so `core.places` imports this
package — and eagerly pulling the profiler in behind it would drag the agent
stack into the write path and close an import cycle. Keys, models and
handles stay eager: they are pure, and they are what the hot paths want.
"""

from typing import TYPE_CHECKING, Any

from .handles import AreaHandle, AreaHandleBuilder, AreaRef
from .keys import (
    decode_area_id,
    encode_area_id,
    is_geo_key,
    is_legacy_geo_key,
    parent_keys,
)
from .models import AreaChip, AreaProfile, NotableSubArea

if TYPE_CHECKING:
    from .profile_service import AreaProfileService
    from .screen_service import AreaScreenService

_LAZY = {
    "AreaProfileService": ".profile_service",
    "AreaScreenService": ".screen_service",
}


def __getattr__(name: str) -> Any:
    """Import the service classes on first use (PEP 562).

    Keeps `from kebi.core.areas import AreaScreenService` working for the
    callers that want it, without every importer of a geo key paying for the
    profiler's dependencies.
    """
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_name, __name__), name)


__all__ = [
    "AreaChip",
    "AreaHandle",
    "AreaHandleBuilder",
    "AreaProfile",
    "AreaProfileService",
    "AreaRef",
    "AreaScreenService",
    "NotableSubArea",
    "decode_area_id",
    "encode_area_id",
    "is_geo_key",
    "is_legacy_geo_key",
    "parent_keys",
]
