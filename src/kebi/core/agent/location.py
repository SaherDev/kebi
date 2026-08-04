"""Working-location model, movement vocabulary, and resolver output schema.

The *working location* is the location a given agent turn operates against —
not necessarily where the user physically is. It is resolved at the start of
every turn by the `resolve_location` graph node (see graph.py).

`WorkingLocation` carries the resolved, geocoded result. Unlike the
all-optional `LocationContext` in `core/places/models.py`, the place fields
here are load-bearing: `country`, `city`, `lat` and `lng` are always present.
`neighborhood` is the single nullable place field — it may be absent only when
the location was the user's actual GPS position and reverse geocoding could not
name the neighbourhood. When the user explicitly named a place, the resolver
holds the result to all five fields (see graph.py validation).

ADR-084 folds *search scope* into the same resolution: every turn also resolves
an effective movement mode and a scope tier, which together yield a concrete
`search_radius_m`. The user's mobility profile (`MovementProfile`, carried in
the `/v1/chat` request) is one input; per-turn context is the other. The scope
fields on `WorkingLocation` default to a neutral area scope — the resolve node
overwrites them every turn, so the defaults are only ever seen by a bare
`WorkingLocation` built outside the node (tests).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, field_validator

if TYPE_CHECKING:
    from kebi.core.config import MovementConfig

LocationSource = Literal["explicit_query", "carried", "user_actual"]

# --- Movement / search-scope vocabulary (ADR-084) --------------------------

# How the user gets around. The profile default lives in NestJS user_settings
# and arrives per-request; a per-turn signal can override it. `motorbike` is a
# first-class mode — it is a primary way to get around in much of the region.
MovementMode = Literal[
    "walking",
    "cycling",
    "motorbike",
    "driving",
    "transit",
    "rideshare",
]

# The user's personal willingness-to-travel baseline. Shifts the scope tier
# ±1 before the tier→metres lookup (see `resolve_radius`).
Reach = Literal["compact", "normal", "far"]

# How wide a turn reaches, around the working point. `walkable` < `neighborhood`
# < `city` < `metro`. A *different* city/country is not a wider tier — it is a
# location shift (the point re-anchors; scope resolves fresh at the new point).
ScopeTier = Literal["walkable", "neighborhood", "city", "metro"]

# The geometry of the search. `area` = a disc around the working point.
# `corridor` = a route between the working point and a destination ("on my
# way home").
ScopeShape = Literal["area", "corridor"]

# How dense the working location is. "Near me" reaches further in a sparse
# town than in a dense city — same tier, different metres (ADR-084). Derived
# from the geocoder's place type, never a static table.
DensityClass = Literal["dense", "medium", "sparse"]

# Ordered low→high for the reach tier-shift in `resolve_radius`.
SCOPE_TIER_ORDER: tuple[ScopeTier, ...] = (
    "walkable",
    "neighborhood",
    "city",
    "metro",
)
_REACH_SHIFT: dict[str, int] = {"compact": -1, "normal": 0, "far": 1}

# Geocoder place types that read as dense / sparse. Anything else is treated
# as medium. The sets carry both vocabularies: Google's (the current
# provider — note its `locality` means any settlement, so it deliberately
# maps to medium, not sparse) and Nominatim's legacy settlement types, kept
# so working locations checkpointed before the provider switch still
# classify. Google exposes no settlement-size signal, so most current
# lookups land on the medium default — an accepted coarsening.
_DENSE_PLACE_TYPES = frozenset(
    {"city", "borough", "city_district", "district", "suburb", "quarter"}
)
_SPARSE_PLACE_TYPES = frozenset(
    {"village", "hamlet", "isolated_dwelling", "farm", "allotments"}
)


def density_class(place_type: str | None) -> DensityClass:
    """Map a geocoder place type to a density class.

    `place_type` is the geocoder's classification of the working location,
    read from the geocode response, not a static table. An unknown or
    missing type degrades to `medium`.
    """
    if not place_type:
        return "medium"
    pt = place_type.strip().lower()
    if pt in _DENSE_PLACE_TYPES or "city" in pt:
        return "dense"
    if pt in _SPARSE_PLACE_TYPES:
        return "sparse"
    return "medium"


class CorridorTarget(BaseModel):
    """One eagerly geocoded stop on the turn's route ("on my way to <name>").

    The resolver names each stop in free text; the resolve node resolves it
    area-first (the persisted entity store) and falls back to a coords-only
    geocode for a POI endpoint — coordinates are all the route geometry
    consumes, so an endpoint with no area entity is handled directly.

    A stop that cannot be resolved at all (an implicit anchor like "home" —
    kebi stores no user addresses) is never recorded here; it triggers a
    location clarification instead. See `CorridorPath` for the ordered chain.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    lat: float
    lng: float


class CorridorPath(BaseModel):
    """The turn's route as an ordered chain of destinations (ADR-136).

    `stops` holds the destinations in the order the user said them; the
    *origin* is the working location itself, so the full polyline is
    `[(working.lat, working.lng), *stops]`. A single-destination trip is
    just a one-stop path — there is no separate one-leg shape.

    Stops are resolved all-or-nothing: a named stop that cannot be resolved
    makes the whole turn a clarification, never a route silently missing one
    of the places the user asked for.
    """

    model_config = ConfigDict(extra="forbid")

    stops: list[CorridorTarget]

    @property
    def destination(self) -> CorridorTarget:
        """The final destination — what the journey is *toward*."""
        return self.stops[-1]

    def points(self, origin_lat: float, origin_lng: float) -> list[tuple[float, float]]:
        """The full polyline, origin first, for the geometry helpers."""
        return [(origin_lat, origin_lng), *((s.lat, s.lng) for s in self.stops)]


class WorkingLocation(BaseModel):
    """A fully-resolved location + search scope a single agent turn operates against."""

    model_config = ConfigDict(extra="forbid")

    country: str
    city: str
    lat: float
    lng: float
    neighborhood: str | None = None
    # ISO-3166 alpha-2, lowercased, from the geocoder's address.country_code.
    # `country` above is the display name ("Vietnam"); canonical entity keys
    # (build_geo_key) need the code. Optional so states checkpointed before
    # this field existed still validate; consumers fall back to resolving the
    # display name.
    country_code: str | None = None

    # Place density of this location, from the geocoder's place type — feeds
    # the radius so "near me" scales with how dense the area is (ADR-084).
    density: str = "medium"
    # The location's bounding box, [min_lat, max_lat, min_lng, max_lng], when
    # the geocoder supplied one. Recorded for a future extent-aware radius
    # refinement; not load-bearing today.
    bbox: list[float] | None = None

    # Search scope (ADR-084). Defaults are neutral — the resolve node sets
    # real values every turn; a turn never reaches the agent with defaults.
    effective_mode: str = "transit"
    scope_tier: str = "city"
    scope_shape: str = "area"
    search_radius_m: float = 0.0
    corridor: CorridorPath | None = None

    @field_validator("corridor", mode="before")
    @classmethod
    def _accept_legacy_single_target(cls, v: object) -> object:
        """Coerce a pre-ADR-136 single `CorridorTarget` into a one-stop path.

        `WorkingLocation` is checkpointed and forbids extra keys, so a
        conversation in flight when this shipped would fail validation on
        resume against the new shape. The coercion is explicit rather than
        tolerated: a dict carrying `lat`/`lng` is the old single destination
        and becomes a one-stop path; anything else passes through to normal
        validation.
        """
        if isinstance(v, dict) and "stops" not in v and "lat" in v and "lng" in v:
            return {"stops": [v]}
        return v


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

    ADR-084 adds the search-scope classification: `scope_tier`, `scope_shape`,
    `effective_mode`, and (for a corridor) `corridor_destinations`. As with
    coordinates, the resolver only *classifies* — `search_radius_m` is derived
    deterministically from config by `resolve_radius`, never emitted here.

    `corridor_destinations` is an **ordered** list (ADR-136): a route can be a
    chain ("Hanoi, then Hue, then Hoi An"), and the order the user said them
    is the order of the journey. This model is per-turn and never
    checkpointed, so its shape carries no back-compatibility burden.
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

    # Search scope (ADR-084).
    scope_tier: ScopeTier = "city"
    scope_shape: ScopeShape = "area"
    effective_mode: MovementMode | None = None
    corridor_destinations: list[str] = []


def resolve_radius(
    mode: str,
    tier: str,
    reach: str,
    density: str,
    cfg: MovementConfig,
) -> float:
    """Deterministic search radius in metres from the resolved scope.

    `radius = radius_tiers[shift(tier, reach)] × mode_multiplier[mode]
              × density_factor[density]`.

    `reach` shifts the tier one step (compact −1, far +1), clamped to the
    tier range. `density` makes the radius location-aware — "near me" reaches
    further in a sparse area than a dense one (ADR-084). Pure function —
    config is passed in, no I/O — so it is testable inline (same shape as
    `compute_confidence`). The resolver LLM never emits this number; it
    classifies `tier`/`mode` and the metres come from config, mirroring
    ADR-083's rule that the resolver never transcribes coordinates.
    """
    try:
        idx = SCOPE_TIER_ORDER.index(tier)
    except ValueError:
        idx = SCOPE_TIER_ORDER.index("city")
    idx = max(0, min(len(SCOPE_TIER_ORDER) - 1, idx + _REACH_SHIFT.get(reach, 0)))
    base = getattr(cfg.radius_tiers, SCOPE_TIER_ORDER[idx])
    multiplier = cfg.mode_multiplier.get(mode, 1.0)
    density_factor = cfg.density_factor.get(density, 1.0)
    return float(base) * float(multiplier) * float(density_factor)
