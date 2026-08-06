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

from kebi.core.places.models import normalize_icon

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

# Geocoder place types that read as dense / sparse. Anything else (notably
# "town") is treated as medium. Kept generous on the dense side — a city's
# districts and suburbs are still city-dense.
_DENSE_PLACE_TYPES = frozenset(
    {"city", "borough", "city_district", "district", "suburb", "quarter"}
)
_SPARSE_PLACE_TYPES = frozenset(
    {"village", "hamlet", "isolated_dwelling", "farm", "locality", "allotments"}
)


def density_class(place_type: str | None) -> DensityClass:
    """Map a geocoder place type to a density class.

    `place_type` is Nominatim's settlement type for the working location
    ("city", "town", "village", …) — read from the geocode response, not a
    static table. An unknown or missing type degrades to `medium`.
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
    """An eagerly geocoded corridor destination ("on my way to <name>").

    The resolver names the destination in free text; the resolve node
    forward-geocodes it. A destination that cannot be geocoded (an implicit
    anchor like "home" — kebi stores no user addresses) is not recorded here;
    it triggers a location clarification instead.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    lat: float
    lng: float


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

    # One emoji per area level, for the `kebi://area/...` links a chat answer
    # carries (ADR-146). A venue's icon rides its catalog row; an area has no
    # row, and the resolver is the one model that already knows which areas
    # this turn is about — so it names them here rather than paying for a
    # second call. Nullable like a venue's: unset means the client falls back
    # to its own mapping.
    city_icon: str | None = None
    neighborhood_icon: str | None = None

    @field_validator("city_icon", "neighborhood_icon")
    @classmethod
    def _normalize_icons(cls, v: str | None) -> str | None:
        return normalize_icon(v)

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
    corridor: CorridorTarget | None = None


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
    `effective_mode`, and (for a corridor) `corridor_destination`. As with
    coordinates, the resolver only *classifies* — `search_radius_m` is derived
    deterministically from config by `resolve_radius`, never emitted here.
    """

    model_config = ConfigDict(extra="forbid")

    country: str | None = None
    city: str | None = None
    neighborhood: str | None = None
    # One emoji for the character of each area this turn resolves to — 🏄 for
    # a surf town, 🗼 for a city its landmark defines (ADR-146). Emitted for
    # every source, not just `explicit_query`: the resolver is handed the
    # carried and actual locations too, so it can name the icon even on the
    # turns where the *names* come from the geocoder.
    city_icon: str | None = None
    neighborhood_icon: str | None = None

    @field_validator("city_icon", "neighborhood_icon")
    @classmethod
    def _normalize_icons(cls, v: str | None) -> str | None:
        return normalize_icon(v)

    source: LocationSource
    is_shift: bool = False
    is_ambiguous: bool = False
    needs_clarification: bool = False
    clarification_reason: str = ""
    # The turn is not about a place at all — a world-knowledge question, a
    # general how-to, chit-chat (ADR-145). Distinct from `needs_clarification`
    # even though both leave the location empty: clarification ends the turn
    # with a question, and asking "which city?" about the World Cup is a
    # non-sequitur that burns the turn. Explicit rather than inferred from an
    # empty result, so "no location found" and "no location wanted" can never
    # be confused.
    location_irrelevant: bool = False

    # Search scope (ADR-084).
    scope_tier: ScopeTier = "city"
    scope_shape: ScopeShape = "area"
    effective_mode: MovementMode | None = None
    corridor_destination: str | None = None


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

    The product is capped at `max_radius_m` (ADR-143). The three factors
    compound, and at the wide end that produced radii no search should ever
    have: metro (45 km) on a motorbike (×2.4) in a sparse area (×1.6) resolved
    to 172.8 km — wider than Bali is long, so every "nearby" search covered the
    whole island and a place an hour in the wrong direction ranked as if it
    were on the way. The cap bounds every combination at once, including ones
    a future tier or mode would introduce, rather than hand-tuning one cell of
    the table. It applies only here: `anchor_to_corridor` widens a route search
    beyond this deliberately, and that width is geometry rather than a guess.
    """
    try:
        idx = SCOPE_TIER_ORDER.index(tier)
    except ValueError:
        idx = SCOPE_TIER_ORDER.index("city")
    idx = max(0, min(len(SCOPE_TIER_ORDER) - 1, idx + _REACH_SHIFT.get(reach, 0)))
    base = getattr(cfg.radius_tiers, SCOPE_TIER_ORDER[idx])
    multiplier = cfg.mode_multiplier.get(mode, 1.0)
    density_factor = cfg.density_factor.get(density, 1.0)
    radius = float(base) * float(multiplier) * float(density_factor)
    return min(radius, float(cfg.max_radius_m))
