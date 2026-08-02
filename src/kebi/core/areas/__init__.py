"""Area layer — the persistent, shared notion of "an area" (Step 2).

`AreaService` is the only resolution path: store first, geocode on
miss, verified round-trip or refused. The entity store holds identity +
geometry only; rich experiential data stays in the knowledge layer,
joined by the shared `entity_key`.
"""

from __future__ import annotations

from kebi.core.areas.models import AreaContext, AreaEntity, AreaKind
from kebi.core.areas.service import AreaService

__all__ = [
    "AreaContext",
    "AreaEntity",
    "AreaKind",
    "AreaService",
]
