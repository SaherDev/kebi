"""Controlled claim-tag vocabulary — shared by the write and read sides.

Claim tags were free-form until now (the harvester emitted arbitrary
lowercase words), so nothing could match on them reliably. This module is
the single bounded vocabulary both sides use: the harvester/curator emit
tags from it (the writer normalizes — keeps known, drops unknown), and the
research read path matches the agent's tags against a claim's tags
value-for-value.

Where a fact about a place/area fits the existing places vocabulary, its
enums are imported so the string values can't drift. The practical/area
types the places vocab lacks — the insider facts harvesting is tuned to
mine — are defined here. Accessibility is categorically excluded (ADR-118):
its enum is deliberately not imported, and the writer drops accessibility
assertions regardless.
"""

from __future__ import annotations

import re
from enum import Enum

from kebi.core.places.tags import (
    AtmosphereTag,
    CuisineTag,
    FeatureTag,
    PriceTag,
    SeasonTag,
    TimeTag,
)


class MoneyTag(str, Enum):
    """Money and payment facts — fees, cash culture, price character."""

    no_fee_atm = "no_fee_atm"
    atm_fees = "atm_fees"
    cash_only = "cash_only"
    card_ok = "card_ok"
    haggling = "haggling"
    cheap_eats = "cheap_eats"
    pricey_area = "pricey_area"
    currency_tip = "currency_tip"


class SafetyTag(str, Enum):
    """Area safety character. Facts about an area, never a guarantee."""

    safe_at_night = "safe_at_night"
    avoid_at_night = "avoid_at_night"
    pickpockets = "pickpockets"
    common_scam = "common_scam"
    tourist_trap = "tourist_trap"
    traffic_caution = "traffic_caution"


class TransportTag(str, Enum):
    """Getting there and around."""

    walkable = "walkable"
    hard_to_reach = "hard_to_reach"
    rideshare = "rideshare"
    metro = "metro"
    taxi_tip = "taxi_tip"
    rental_scooter = "rental_scooter"
    parking_hard = "parking_hard"


class EtiquetteTag(str, Enum):
    """Local customs and expectations."""

    tipping = "tipping"
    no_tipping = "no_tipping"
    dress_code = "dress_code"
    reservation_needed = "reservation_needed"
    walk_in_ok = "walk_in_ok"
    remove_shoes = "remove_shoes"
    local_custom = "local_custom"


class ExperienceTag(str, Enum):
    """Experience types an area offers — the interest signal a noted
    route/region collapses to (location-kinds Step 2). A named route is
    never its own entity; its claims land on the containing area tagged
    with the kind of experience it stands for."""

    scenic_route = "scenic_route"
    road_trip = "road_trip"
    motorbike_route = "motorbike_route"
    hiking = "hiking"
    trekking = "trekking"
    diving_snorkeling = "diving_snorkeling"
    surfing = "surfing"
    island_hopping = "island_hopping"


class TimingTrickTag(str, Enum):
    """When to go — the tricks a local knows."""

    go_early = "go_early"
    go_late = "go_late"
    avoid_weekends = "avoid_weekends"
    weekday_best = "weekday_best"
    sunset_spot = "sunset_spot"
    sunrise_spot = "sunrise_spot"
    long_queue = "long_queue"
    seasonal_closure = "seasonal_closure"


# Type label → enum, in prompt-render order. The labels mirror
# places.tags.TagType naming for the reused enums.
CLAIM_TAG_TYPES: dict[str, type[Enum]] = {
    "cuisine": CuisineTag,
    "atmosphere": AtmosphereTag,
    "price": PriceTag,
    "time": TimeTag,
    "season": SeasonTag,
    "feature": FeatureTag,
    "money": MoneyTag,
    "safety": SafetyTag,
    "transport": TransportTag,
    "etiquette": EtiquetteTag,
    "timing_trick": TimingTrickTag,
    "experience": ExperienceTag,
}


def _fold(value: str) -> str:
    """Match key for a tag: casefolded, runs of spaces/hyphens/underscores
    collapsed to one underscore — so "No-Fee ATM" and "no_fee_atm" meet."""
    return re.sub(r"[\s\-_]+", "_", value.strip().casefold())


# Folded value → canonical value, across every enum in the vocabulary.
_CANONICAL: dict[str, str] = {
    _fold(member.value): member.value
    for enum_cls in CLAIM_TAG_TYPES.values()
    for member in enum_cls
}

CLAIM_TAG_VALUES: frozenset[str] = frozenset(_CANONICAL.values())

# The experience-type values on their own — a route/region share's interest
# collapses to these, and Step 3 lifts them out of the harvested claims into
# an experience taste signal.
EXPERIENCE_TAG_VALUES: frozenset[str] = frozenset(m.value for m in ExperienceTag)


def normalize_claim_tags(tags: list[str]) -> list[str]:
    """Map raw tags onto the vocabulary: keep known ones in canonical form,
    drop unknown ones, dedupe preserving order.

    The writer's backstop against off-vocab hallucinations polluting the tag
    index — mirrors its drop-don't-mis-key discipline (ADR-126).
    """
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in tags:
        canonical = _CANONICAL.get(_fold(raw))
        if canonical is not None and canonical not in seen:
            seen.add(canonical)
            normalized.append(canonical)
    return normalized


def render_claim_tag_vocabulary() -> str:
    """The vocabulary as prompt text — appended to the harvester/curator
    system prompts so the emitting model and this module can never drift."""
    lines = ["Allowed tag values (use ONLY these; anything else is discarded):"]
    for type_name, enum_cls in CLAIM_TAG_TYPES.items():
        values = ", ".join(member.value for member in enum_cls)
        lines.append(f"- {type_name}: {values}")
    return "\n".join(lines)
