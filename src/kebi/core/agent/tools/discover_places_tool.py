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
- Memory constraints flow in via the `tags` arg (agent extracts
  them from `{memory_summary}`). Safety values (dietary/accessibility)
  are enforced post-fetch via the shared `hard_constraints_satisfied`
  filter; all other tag values only bias retrieval (ADR-118).

Location anchoring is a hard precondition: with no working_location
lat/lng + positive `search_radius_m` the tool returns immediately with
`empty_reason="no_location"` and never calls the provider.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Annotated, Any

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
from kebi.core.agent.tools._area_anchor import (
    SearchTarget,
    anchors_from_state,
    gather_targets,
    journey_from_state,
    search_targets,
)
from kebi.core.agent.tools._corridor import (
    filter_and_order,
    is_corridor,
    is_route_too_long,
    place_coords,
    route_summary,
    waypoint_contexts,
)
from kebi.core.agent.tools._hard_constraints import (
    hard_constraints_satisfied,
    split_constraints,
)
from kebi.core.agent.tools._scope import clamp_to_walkable_for_utility
from kebi.core.agent.tools._search_args import (
    AREA_KEYS_DESC,
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
    NOTHING_ON_ROUTE,
    ROUTE_TOO_LONG,
    TITLES,
    found_summary,
)
from kebi.core.agent.tools._with_timeout import tool_step_base_id, with_timeout
from kebi.core.agent.tools._working_location import (
    is_anchored,
    maybe_working_location,
)
from kebi.core.agent.tools.consult_models import ConsultCandidate, ConsultResult
from kebi.core.areas.models import AreaEntity
from kebi.core.config import get_config
from kebi.core.extraction.extraction_pipeline import SearchServiceFactory
from kebi.core.places.models import (
    LocationContext,
    PlaceCategory,
    PlaceObject,
    PlaceQuery,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from kebi.core.config import MovementConfig

logger = logging.getLogger(__name__)

_TOOL_NAME = "discover_places"


async def _search_contexts(
    *,
    places_search_factory: SearchServiceFactory,
    queries: list[PlaceQuery],
    limit: int,
    concurrency: int,
) -> list[PlaceObject]:
    """Run each query concurrently and return the deduped union, in order.

    One query on an ordinary turn; one per sampled waypoint on a route. **Each
    concurrent lookup opens its own `PlacesSearchService` via the factory** so
    each gets its own `AsyncSession` — SQLAlchemy / asyncpg sessions are not
    concurrency-safe, the same rule `suggest_places._validate_candidates`
    follows.

    Waypoint discs overlap, so the same place surfaces from several points;
    dedup keeps the first occurrence, which is the earliest waypoint along the
    route. A *partial* failure is survivable — a route answer missing one
    waypoint still beats no answer — so a failed lookup is logged and skipped.
    Only a total failure raises, which the caller reports as a provider error.
    """
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _run(query: PlaceQuery) -> list[PlaceObject]:
        async with sem, places_search_factory() as svc:
            return await svc.find(query, limit=limit)

    outcomes = await asyncio.gather(*(_run(q) for q in queries), return_exceptions=True)

    results: list[PlaceObject] = []
    seen: set[str] = set()
    failures: list[BaseException] = []
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            failures.append(outcome)
            continue
        for place in outcome:
            key = place.provider_id or place.id or place.place_name
            if key in seen:
                continue
            seen.add(key)
            results.append(place)

    if failures and len(failures) == len(outcomes):
        raise failures[0]
    if failures:
        logger.warning(
            "discover_places: %d of %d waypoint lookups failed",
            len(failures),
            len(outcomes),
        )
    return results


async def _discover_at_areas(
    *,
    places_search_factory: SearchServiceFactory,
    state: AgentState,
    tool_call_id: str,
    anchors: list[AreaEntity],
    journey: bool,
    query: str,
    categories: list[PlaceCategory] | None,
    tags: list[str] | None,
    limit: int,
    movement_cfg: MovementConfig,
    concurrency: int,
    steps: list[ReasoningStep],
    finish: Callable[..., None],
    trace: Callable[[str, str], None],
) -> Command[Any]:
    """Discovery anchored on each area the agent named (ADR-140).

    One provider search per target — each named area, plus the stretches
    between them when the agent said people drive it. Results are tagged with
    where they came from and capped per target so a single dense city cannot
    crowd out the others: an answer about four neighborhoods that returns
    twelve places in one of them has not answered the question.

    The free-text query is dropped for the same reason it is dropped at a route
    waypoint: the provider treats text search's location as a soft bias, so a
    text query returns whatever is prominent in the wider region rather than
    what is actually inside the area. Categories and tags still steer it.
    """
    targets = search_targets(
        anchors,
        movement_cfg,
        journey=journey,
        max_areas=get_config().agent.area_anchor.max_areas,
    )
    if not targets:
        finish("nothing to check", kind="no_match")
        return _build_command(
            state=state,
            tool_call_id=tool_call_id,
            result=ConsultResult(candidates=[], empty_reason="no_match"),
            steps=steps,
        )
    stretches = sum(1 for t in targets if t.area is None)
    trace(
        "start",
        f"checking {len(targets) - stretches} areas"
        + (f" and {stretches} points on the way" if stretches else ""),
    )
    per_target = max(1, -(-limit // len(targets)))

    async def _run(target: SearchTarget) -> list[PlaceObject]:
        async with places_search_factory() as svc:
            return await svc.find(
                PlaceQuery(categories=categories, tags=tags, location=target.context),
                limit=per_target,
            )

    try:
        tagged = await gather_targets(
            targets,
            concurrency=concurrency,
            runner=_run,
            dedup_key=lambda p: p.provider_id or p.id or p.place_name,
        )
    except Exception as exc:
        logger.warning("discover_places area lookup failed: %s", exc, exc_info=True)
        finish("place search hit an error", kind="provider_error")
        return _build_command(
            state=state,
            tool_call_id=tool_call_id,
            result=ConsultResult(candidates=[], empty_reason="error"),
            steps=steps,
        )

    hard, _soft = split_constraints(tags or [])
    kept = [(t, p) for t, p in tagged if hard_constraints_satisfied(p, hard)]
    dropped = len(tagged) - len(kept)

    if not kept:
        finish(
            NONE_FIT if dropped else "nothing turned up in those areas",
            kind="constraints_drop" if dropped else "no_match",
        )
        return _build_command(
            state=state,
            tool_call_id=tool_call_id,
            result=ConsultResult(candidates=[], empty_reason="no_match"),
            steps=steps,
        )

    final = kept[:limit]
    finish(
        found_summary([p.place_name for _, p in final], dropped=dropped if hard else 0)
    )
    return _build_command(
        state=state,
        tool_call_id=tool_call_id,
        result=ConsultResult(
            candidates=[
                ConsultCandidate(
                    place=place.to_core(),
                    user_data=None,
                    source="discovered",
                    rrf_score=0.0,
                    anchor_area_key=target.group_key,
                )
                for target, place in final
            ],
            empty_reason=None,
        ),
        steps=steps,
    )


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
        area_keys: Annotated[
            list[str] | None, Field(description=AREA_KEYS_DESC)
        ] = None,
        limit: Annotated[int | None, Field(description=LIMIT_DESC)] = None,
    ) -> Command[Any]:
        """Provider-driven place search, at named areas or the working location."""
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
                area_keys=area_keys,
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
    area_keys: list[str] | None = None,
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
            area_keys=area_keys,
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
    area_keys: list[str] | None = None,
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

    movement_cfg = get_config().movement
    concurrency = get_config().agent.suggest_places.provider_concurrency

    # Area-anchored turn (ADR-140) takes precedence over the working location:
    # the agent named these areas, so "around here" means "at each of them".
    # It needs no working location at all — the anchors carry their own
    # coordinates, which is what lets "which neighborhood in Hoi An?" answer
    # from Da Nang.
    anchors = anchors_from_state(state, area_keys)
    if anchors:
        return await _discover_at_areas(
            places_search_factory=places_search_factory,
            state=state,
            tool_call_id=tool_call_id,
            anchors=anchors,
            journey=journey_from_state(state),
            query=query,
            categories=categories,
            tags=tags,
            limit=limit,
            movement_cfg=movement_cfg,
            concurrency=concurrency,
            steps=steps,
            finish=_finish,
            trace=_trace,
        )

    working = maybe_working_location(state)
    if not is_anchored(working):
        _finish(NEED_LOCATION, kind="no_location")
        return _build_command(
            state=state,
            tool_call_id=tool_call_id,
            result=ConsultResult(candidates=[], empty_reason="no_location"),
            steps=steps,
        )

    assert working is not None  # narrowed by _is_anchored
    # Utility errands are walked to — clamp to a walkable radius (same rule as
    # suggest_places) so this fallback can't reintroduce a far result. On a
    # route the clamp narrows the corridor's half-width, which is the right
    # meaning for "an ATM on the way".
    working = clamp_to_walkable_for_utility(working, categories, movement_cfg)

    # Route-shaped turn (ADR-136) — the length gate first, so an all-oversized
    # route spends no provider call at all.
    on_route = is_corridor(working)
    if on_route and is_route_too_long(working, movement_cfg):
        _finish(ROUTE_TOO_LONG, kind="route_too_long")
        return _build_command(
            state=state,
            tool_call_id=tool_call_id,
            result=ConsultResult(candidates=[], empty_reason="route_too_long"),
            steps=steps,
        )

    if on_route:
        _trace("start", f"checking along {route_summary(working)}")
    else:
        _trace("start", f"checking around {_location_label(working)}")

    # One search per sampled waypoint on a route (capped by config), a single
    # search around the working point otherwise.
    contexts = (
        waypoint_contexts(working, movement_cfg)
        if on_route
        else [_build_location_context(working)]
    )

    def _query_at(location: LocationContext) -> PlaceQuery:
        # At a route waypoint the free-text query is dropped on purpose. The
        # provider treats text search's location as a soft *bias*, so a text
        # query at a waypoint returns whatever is most prominent in the wider
        # region rather than what is actually there — searching "viewpoint"
        # 6 km from the Hai Van summit returns Da Nang's riverfront
        # viewpoints, 20 km back down the road. Dropping the text routes the
        # call to nearby search, whose location is a hard restriction, so the
        # results are genuinely at the waypoint. Categories and tags still
        # steer it, and the whole point of sampling waypoints is coverage of
        # the route, which a drifting text search silently destroys.
        return PlaceQuery(
            place_names=None if on_route else ([query] if query else None),
            categories=categories,
            tags=tags,
            location=location,
        )

    try:
        hits = await _search_contexts(
            places_search_factory=places_search_factory,
            queries=[_query_at(ctx) for ctx in contexts],
            limit=limit,
            concurrency=get_config().agent.suggest_places.provider_concurrency,
        )
    except Exception as exc:
        logger.warning("discover_places provider lookup failed: %s", exc, exc_info=True)
        _finish("place search hit an error", kind="provider_error")
        return _build_command(
            state=state,
            tool_call_id=tool_call_id,
            result=ConsultResult(candidates=[], empty_reason="error"),
            steps=steps,
        )

    # Administrative areas (cities, districts, roads) are rejected upstream at
    # validation (`_google_mapper`, ADR-082), so the provider never returns them.
    venues = hits
    if on_route:
        # Waypoint discs overlap and each one is a circle, so the union carries
        # places that sit near a sample point but not near the route. Drop
        # those, then order what survives origin → final destination: this is
        # what turns a set of places into a journey.
        venues = filter_and_order(venues, working, movement_cfg, coords=place_coords)

    if not venues:
        _finish(
            NOTHING_ON_ROUTE if on_route else "nothing nearby matched that",
            kind="no_match",
        )
        return _build_command(
            state=state,
            tool_call_id=tool_call_id,
            result=ConsultResult(candidates=[], empty_reason="no_match"),
            steps=steps,
        )

    # Safety values (dietary/accessibility) exclude; everything else is a
    # preference signal that already biased retrieval via PlaceQuery.tags
    # (DB predicate + provider keyword text) — a fresh place with no
    # experiential tags yet must not be zeroed out (ADR-118).
    hard, _soft = split_constraints(tags or [])
    filtered = [p for p in venues if hard_constraints_satisfied(p, hard)]
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
    _finish(found_summary(final_names, dropped=dropped if hard else 0))

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
