"""`discover_places` agent tool — provider-driven nearby/area search.

Third member of the consult-family trio. Where `find_saved` reads the
user's own collection and `suggest_places` asks an LLM to name famous
places, `discover_places` calls the place provider directly for the
turn's working location + category + filters. No LLM in the loop, no
saved-collection lookup.

It is the **fall-through floor**, not a front-line tool — decided by
the agent prompt. When `find_saved` and/or `suggest_places` returned
`empty_reason="no_match"` earlier in the same turn, the agent calls
`discover_places` to recover with whatever the catalog or Google can
surface near the working location.

This includes utility errands (pharmacy, ATM, supermarket, …). Those
no longer route here directly: the agent sends them to `suggest_places`
first, where the namer proposes the trusted brand/chain for the country
and the provider resolves the nearest branch — an opinionated pick. Only
when that path names no credible brand or none validates nearby does
`discover_places` step in with the generic nearest match.

Mechanics:

- Single `PlacesSearchService.find()` call. DB-first; Google fallback
  only fires on an empty DB hit (ADR-070 caching is reused unchanged).
- Anchor is `WorkingLocation.lat/lng` with `LocationContext.radius_m =
  int(working.search_radius_m)`. Scope (walkable / neighborhood / city
  / metro) was already classified by the `resolve_location` LLM node
  and reduced to a concrete radius by `resolve_radius` — this tool
  never re-classifies.
- Named-area overrides (`neighborhood`/`city`/`country`) are accepted
  for arg-schema parity with the other tools and **ignored** at
  runtime — same rule `suggest_places` follows. Location shifts (the
  "in Shinjuku while in Bangkok" case) belong to the resolver, not to
  individual tools.
- Memory hard constraints flow in via the `tags` arg (agent extracts
  them from `{memory_summary}`) and are enforced with the shared
  `hard_constraints_satisfied` filter after the provider returns.

Location anchoring is a hard precondition: with no working_location
lat/lng + positive `search_radius_m` the tool returns immediately with
`empty_reason="no_location"` and never calls the provider.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from pydantic import Field

from kebi.core.agent._trace_context import set_tool
from kebi.core.agent.location import WorkingLocation
from kebi.core.agent.reasoning import ReasoningStep
from kebi.core.agent.state import AgentState
from kebi.core.agent.stream_emit import emit_step_active, emit_step_done
from kebi.core.agent.tools._hard_constraints import hard_constraints_satisfied
from kebi.core.agent.tools._scope import clamp_to_walkable_for_utility
from kebi.core.agent.tools._search_args import (
    CATEGORIES_DESC,
    CITY_DESC,
    COUNTRY_DESC,
    LIMIT_DESC,
    NEIGHBORHOOD_DESC,
    QUERY_DESC,
    TAGS_DESC,
)
from kebi.core.agent.tools._summaries import (
    NEED_LOCATION,
    NONE_FIT,
    TITLES,
    found_summary,
)
from kebi.core.agent.tools._with_timeout import tool_step_base_id, with_timeout
from kebi.core.agent.tools.consult_models import ConsultCandidate, ConsultResult
from kebi.core.config import get_config
from kebi.core.extraction.extraction_pipeline import SearchServiceFactory
from kebi.core.extraction.geo_filter import drop_geographic_features
from kebi.core.places.models import (
    LocationContext,
    PlaceCategory,
    PlaceQuery,
)

logger = logging.getLogger(__name__)

_TOOL_NAME = "discover_places"


def _maybe_working_location(state: AgentState) -> WorkingLocation | None:
    """Read the turn's working location off state, returning None on absence."""
    wl_dict = state.get("working_location")
    if not wl_dict:
        return None
    try:
        return WorkingLocation.model_validate(wl_dict)
    except Exception:
        logger.warning("working_location on state failed validation; ignoring")
        return None


def _is_anchored(working: WorkingLocation | None) -> bool:
    """Strict location-anchoring gate for the provider phase.

    Mirrors `suggest_places._is_anchored`. The provider's
    locationBias.circle / locationRestriction.circle needs lat/lng +
    radius_m — and `WorkingLocation.search_radius_m` defaults to 0.0
    before the resolver has run, so a positive radius is the
    "resolver has decided this turn" signal. A turn that lacks either
    is a `no_location` outcome.
    """
    if working is None:
        return False
    return working.search_radius_m > 0


def _build_location_context(working: WorkingLocation) -> LocationContext:
    """Bound the provider call to the resolved working location.

    Carries lat/lng + radius_m (required by
    `PlaceQuery._validate_geo_location`) plus city/country/neighborhood
    so the provider's text-search reranker has the named context too.
    `_is_anchored` has already gated this — radius_m is always positive
    here.
    """
    return LocationContext(
        lat=working.lat,
        lng=working.lng,
        radius_m=int(working.search_radius_m),
        city=working.city,
        country=working.country,
        neighborhood=working.neighborhood,
    )


def _location_label(working: WorkingLocation) -> str:
    """One short phrase naming the location for user-facing reasoning steps."""
    if working.neighborhood:
        return f"{working.neighborhood}, {working.city}"
    return working.city


def _make_step(step_id: str, summary: str) -> ReasoningStep:
    """Build a debug-only internal narration step from this tool's namespace.

    User-facing output is the single `base_id` outcome row (ADR-103); these
    phase steps ride the stream as `debug` for tracing only.
    """
    return ReasoningStep(
        step=f"{_TOOL_NAME}.{step_id}",
        summary=summary,
        source="agent",
        visibility="debug",
        duration_ms=0.0,
    )


def _build_command(
    *,
    state: AgentState,
    tool_call_id: str,
    result: ConsultResult,
    steps: list[ReasoningStep],
) -> Command[Any]:
    """Pack the tool's result + reasoning steps into one Command."""
    tool_msg = ToolMessage(
        content=result.model_dump_json(),
        tool_call_id=tool_call_id,
        name=_TOOL_NAME,
    )
    return Command(
        update={
            "messages": [tool_msg],
            "reasoning_steps": (state.get("reasoning_steps") or []) + steps,
            "tool_calls_used": state.get("tool_calls_used", 0) + 1,
        }
    )


def build_discover_places_tool(
    places_search_factory: SearchServiceFactory,
) -> BaseTool:
    """Factory — bind the request-scoped search-service factory into the tool.

    `places_search_factory` opens a fresh `AsyncSession` per call. This
    tool only ever makes one `find()` call per invocation, but reusing
    the factory keeps the session-ownership model uniform across the
    consult-family tools and avoids holding a session beyond the call.
    """

    @tool(_TOOL_NAME)
    async def discover_places(
        query: Annotated[str, Field(description=QUERY_DESC)],
        tool_call_id: Annotated[str, InjectedToolCallId],
        state: Annotated[AgentState, InjectedState],
        categories: Annotated[
            list[PlaceCategory] | None, Field(description=CATEGORIES_DESC)
        ] = None,
        tags: Annotated[list[str] | None, Field(description=TAGS_DESC)] = None,
        neighborhood: Annotated[  # noqa: ARG001 - see _run_discover_places docstring
            str | None, Field(description=NEIGHBORHOOD_DESC)
        ] = None,
        city: Annotated[  # noqa: ARG001
            str | None, Field(description=CITY_DESC)
        ] = None,
        country: Annotated[  # noqa: ARG001
            str | None, Field(description=COUNTRY_DESC)
        ] = None,
        limit: Annotated[int | None, Field(description=LIMIT_DESC)] = None,
    ) -> Command[Any]:
        """Provider-driven place search anchored at the working location."""
        cfg = get_config().agent
        timeout_s = cfg.tool_timeouts_seconds.discover_places
        default_limit = cfg.discover_places.default_limit
        max_limit = cfg.discover_places.max_limit
        effective_limit = max(1, min(limit or default_limit, max_limit))

        return await with_timeout(
            tool_name=_TOOL_NAME,
            tool_call_id=tool_call_id,
            seconds=timeout_s,
            state=state,
            coro=_run_discover_places(
                places_search_factory=places_search_factory,
                state=state,
                tool_call_id=tool_call_id,
                query=query,
                categories=categories,
                tags=tags,
                limit=effective_limit,
            ),
        )

    return discover_places


async def _run_discover_places(
    *,
    places_search_factory: SearchServiceFactory,
    state: AgentState,
    tool_call_id: str,
    query: str,
    categories: list[PlaceCategory] | None,
    tags: list[str] | None,
    limit: int,
) -> Command[Any]:
    """Inner body — issues the single provider call. Wrapped by with_timeout."""
    # ContextVar set here so Google Places spans inherit tool=discover_places
    # once subtask 3 instruments them. discover_places has no in-scope paid
    # calls in subtask 1 — this is intentional dead state, not unused code.
    with set_tool(_TOOL_NAME):
        return await _run_discover_places_impl(
            places_search_factory=places_search_factory,
            state=state,
            tool_call_id=tool_call_id,
            query=query,
            categories=categories,
            tags=tags,
            limit=limit,
        )


async def _run_discover_places_impl(
    *,
    places_search_factory: SearchServiceFactory,
    state: AgentState,
    tool_call_id: str,
    query: str,
    categories: list[PlaceCategory] | None,
    tags: list[str] | None,
    limit: int,
) -> Command[Any]:
    """The agent-supplied area overrides (neighborhood / city / country)
    are accepted on the outer `@tool` signature for arg-schema parity
    with `find_saved` / `suggest_places`, but the provider phase needs
    coords. Location shifts (travel intent, named city/neighborhood)
    are handled by the `resolve_location` node, which re-anchors
    `working_location` per turn — so this tool just reads whatever the
    resolver decided. Overrides are dropped silently in the outer
    wrapper.
    """
    steps: list[ReasoningStep] = []
    base_id = tool_step_base_id(_TOOL_NAME, state)
    # One user-visible row for the whole call (ADR-103): a single skeleton now,
    # filled with the outcome by `_finish` below. The skeleton spans the
    # provider latency, so no per-phase user steps. Internal narration rides as
    # debug for tracing.
    outcome_started = emit_step_active(
        base_id, _TOOL_NAME, title=TITLES[_TOOL_NAME], source="agent"
    )

    def _trace(step_id: str, summary: str) -> None:
        """Emit a debug-only internal narration step (tracing, not a user row)."""
        step = _make_step(step_id, summary)
        sid = f"{base_id}.{step_id}"
        started = emit_step_active(
            sid, step.step, title="", source="agent", visibility="debug"
        )
        emit_step_done(sid, step, started=started)
        steps.append(step)

    def _finish(summary: str, *, kind: str = "summary") -> None:
        """Emit the single user-visible outcome row under `base_id`.

        `kind` names the outcome (summary / no_location / no_match / …) as the
        machine `step` id; the user only ever sees the shared title + summary.
        """
        step = ReasoningStep(
            step=f"{_TOOL_NAME}.{kind}",
            title=TITLES[_TOOL_NAME],
            summary=summary,
            source="agent",
            visibility="user",
            duration_ms=0.0,
        )
        emit_step_done(base_id, step, started=outcome_started)
        steps.append(step)

    working = _maybe_working_location(state)
    if not _is_anchored(working):
        _finish(NEED_LOCATION, kind="no_location")
        return _build_command(
            state=state,
            tool_call_id=tool_call_id,
            result=ConsultResult(candidates=[], empty_reason="no_location"),
            steps=steps,
        )

    assert working is not None  # narrowed by _is_anchored
    # Utility errands are walked to — clamp to a walkable radius (same rule as
    # suggest_places) so this fallback can't reintroduce a far result.
    working = clamp_to_walkable_for_utility(working, categories, get_config().movement)
    location_label = _location_label(working)
    _trace("start", f"checking around {location_label}")

    place_loc = _build_location_context(working)
    place_query = PlaceQuery(
        place_names=[query] if query else None,
        categories=categories,
        tags=tags,
        location=place_loc,
    )

    try:
        async with places_search_factory() as svc:
            hits = await svc.find(place_query, limit=limit)
    except Exception as exc:
        logger.warning("discover_places provider lookup failed: %s", exc, exc_info=True)
        _finish("place search hit an error", kind="provider_error")
        return _build_command(
            state=state,
            tool_call_id=tool_call_id,
            result=ConsultResult(candidates=[], empty_reason="error"),
            steps=steps,
        )

    venues = drop_geographic_features(hits)

    if not venues:
        _finish("nothing nearby matched that", kind="no_match")
        return _build_command(
            state=state,
            tool_call_id=tool_call_id,
            result=ConsultResult(candidates=[], empty_reason="no_match"),
            steps=steps,
        )

    required = tags or []
    filtered = [p for p in venues if hard_constraints_satisfied(p, required)]
    dropped = len(venues) - len(filtered)

    if not filtered:
        _finish(NONE_FIT, kind="constraints_drop")
        return _build_command(
            state=state,
            tool_call_id=tool_call_id,
            result=ConsultResult(candidates=[], empty_reason="no_match"),
            steps=steps,
        )

    final_objs = filtered[:limit]
    final_names = [p.place_name for p in final_objs]
    _finish(found_summary(final_names, dropped=dropped if required else 0))

    candidates = [
        ConsultCandidate(
            place=obj.to_core(),
            user_data=None,
            source="discovered",
            rrf_score=0.0,
        )
        for obj in final_objs
    ]

    return _build_command(
        state=state,
        tool_call_id=tool_call_id,
        result=ConsultResult(candidates=candidates, empty_reason=None),
        steps=steps,
    )
