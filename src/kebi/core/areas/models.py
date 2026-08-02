"""Area-layer domain models (location-kinds Step 2).

`AreaEntity` is identity + geometry only — the entity store never holds
experiential data; that stays in the knowledge layer keyed by the same
`entity_key` (ADR-118 spirit).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

AreaKind = Literal["country", "city", "neighborhood"]


class AreaEntity(BaseModel):
    """A verified geographic area — the shared notion every subsystem keys on.

    `entity_key` is the `build_geo_key` format (`vn`, `vn/hoi-an`) — the
    same key knowledge claims carry, so claims and entities join without
    translation. `bbox` is `[min_lat, max_lat, min_lng, max_lng]`.
    """

    model_config = ConfigDict(frozen=True)

    entity_key: str
    entity_type: AreaKind
    name: str
    aliases: list[str] = []
    country_code: str
    lat: float
    lng: float
    bbox: list[float] | None = None
    place_type: str | None = None
    parent_key: str | None = None
    provider_id: str | None = None
    geo_refreshed_at: datetime | None = None


class AreaContext(BaseModel):
    """The location context a noted non-venue name arrived with (ADR-082
    per-candidate area): where the share placed it, used to resolve the
    containing area when the name itself is not an area."""

    model_config = ConfigDict(frozen=True)

    city: str | None = None
    country: str | None = None
    country_code: str | None = None
