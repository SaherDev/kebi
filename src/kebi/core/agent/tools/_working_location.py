"""Shared working-location readers for consult-family agent tools.

Every tool reads the turn's resolved `working_location` off state the same
way; the copies used to live per-tool. `maybe_working_location` tolerates
absence and validation failure (a tool degrades to `no_location`, never
crashes the turn). `is_anchored` is the strict gate the provider-facing
tools use: the provider's location bias needs lat/lng + a positive radius,
and `search_radius_m` is only positive once the resolver has decided this
turn.
"""

from __future__ import annotations

import logging

from kebi.core.agent.location import WorkingLocation
from kebi.core.agent.state import AgentState

logger = logging.getLogger(__name__)


def maybe_working_location(state: AgentState) -> WorkingLocation | None:
    """Read the turn's working location off state, returning None on absence."""
    wl_dict = state.get("working_location")
    if not wl_dict:
        return None
    try:
        return WorkingLocation.model_validate(wl_dict)
    except Exception:
        logger.warning("working_location on state failed validation; ignoring")
        return None


def is_anchored(working: WorkingLocation | None) -> bool:
    """Strict location-anchoring gate for a provider phase.

    The provider's locationBias.circle / locationRestriction.circle needs
    lat/lng + radius_m — and `WorkingLocation.search_radius_m` defaults to
    0.0 before the resolver has run, so a positive radius is the "resolver
    has decided this turn" signal. A turn that lacks either is a
    `no_location` outcome.
    """
    if working is None:
        return False
    return working.search_radius_m > 0
