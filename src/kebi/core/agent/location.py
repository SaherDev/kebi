"""Working-location model and resolver structured-output schema.

The *working location* is the location a given agent turn operates against —
not necessarily where the user physically is. It is resolved at the start of
every turn by the `resolve_location` graph node (see graph.py).

`WorkingLocation` carries the resolved, geocoded result. Unlike the
all-optional `LocationContext` in `core/places/models.py`, the fields here are
load-bearing: `country`, `city`, `lat` and `lng` are always present.
`neighborhood` is the single nullable field — it may be absent only when the
location was the user's actual GPS position and reverse geocoding could not
name the neighbourhood. When the user explicitly named a place, the resolver
holds the result to all five fields (see graph.py validation).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

LocationSource = Literal["explicit_query", "carried", "user_actual"]


class WorkingLocation(BaseModel):
    """A fully-resolved location a single agent turn operates against."""

    model_config = ConfigDict(extra="forbid")

    country: str
    city: str
    lat: float
    lng: float
    neighborhood: str | None = None


class LocationResolution(BaseModel):
    """Structured output of the location-resolver LLM call.

    The resolver decides *which* location the current turn is about — it
    applies the priority rule (explicit in query > carried from conversation
    > the user's actual location) and the shift detector, and picks a
    `source`. It does not produce coordinates: the node derives those
    deterministically (forward-geocode a named place, take the user's actual
    GPS, or reuse the carried location), never trusting the LLM to transcribe
    numbers. `country`/`city`/`neighborhood` are filled only for an
    explicitly-named place (`source == "explicit_query"`); for `carried` and
    `user_actual` the node supplies the place names.
    """

    model_config = ConfigDict(extra="forbid")

    country: str | None = None
    city: str | None = None
    neighborhood: str | None = None
    source: LocationSource
    is_shift: bool = False
    is_ambiguous: bool = False
    needs_clarification: bool = False
    clarification_reason: str = ""
