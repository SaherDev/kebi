"""`suggest_places` agent tool — propose, validate, and return new places.

Sibling tool to `find_saved`. Where `find_saved` searches the user's own
saved collection, `suggest_places` answers the question "what good
places exist around here that the user has NOT saved yet?". It does this
in two phases inside one tool turn:

  1. Ask the configured `candidate_namer` LLM for a short list of real,
     well-known place names that fit the intent + working location +
     hard constraints. The LLM never returns coordinates or structured
     places — only names and a one-line reason per name.

  2. Validate each name against the place provider via the shared
     `PlacesSearchService.find()` (DB-first, cache overlay, Google
     fallback). The provider's locationBias.circle bounds every call to
     the working location's search radius, so out-of-scope names
     self-eliminate. Names with no provider hit are dropped.

After validation a post-fetch tag filter applies the hard constraints
again — the LLM bias gets the namer most of the way; the explicit filter
catches the rest. Result is `ConsultResult` with `source="suggested"`
candidates carrying the LLM's `reason` per candidate (a new field on
`ConsultCandidate`).

Location anchoring is a hard precondition: with no working_location
lat/lng + radius this tool returns immediately with
`empty_reason="no_location"` and makes neither an LLM call nor a
provider call. Search scope (radius, density, mode) is whatever the
`resolve_location` node decided this turn — never overridden here.

Arg schema is byte-identical to `find_saved` (shared module
`_search_args`) so the agent picks between the two tools on semantics
alone. The user sees one reasoning row for the whole call (ADR-103): the
"suggested a few spots" action with the outcome result line under it;
the internal name/validate phases ride the stream as `debug` for tracing.
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
    anchor_radius_m,
    anchors_from_state,
    attribute_to_area,
    capped,
    enclosing_anchor_context,
)
from kebi.core.agent.tools._corridor import (
    enclosing_context,
    filter_and_order,
    is_corridor,
    is_route_too_long,
    place_coords,
    route_summary,
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
    PLACE_NAMES_DESC,
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
from kebi.core.agent.tools.candidate_namer import (
    CandidateName,
    CandidateNamerService,
)
from kebi.core.agent.tools.consult_models import ConsultCandidate, ConsultResult
from kebi.core.areas.models import AreaEntity
from kebi.core.areas.suggestion_service import AreaSuggestionService
from kebi.core.config import get_config
from kebi.core.extraction.candidate_mapper import normalize_query
from kebi.core.extraction.extraction_pipeline import SearchServiceFactory
from kebi.core.knowledge.schemas import _slugify
from kebi.core.places.models import (
    LocationContext,
    PlaceCategory,
    PlaceCore,
    PlaceObject,
    PlaceQuery,
    normalize_icon,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from kebi.core.config import MovementConfig

logger = logging.getLogger(__name__)

_TOOL_NAME = "suggest_places"


def _build_location_context(working: WorkingLocation) -> LocationContext:
    """Bound every provider call to the resolved working location.

    Carries lat/lng + radius_m (required by `PlaceQuery._validate_geo_location`)
    plus city/country/neighborhood so the provider's text-search reranker
    has the named context too. `is_anchored` has already gated this —
    radius_m is always positive here.
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
    phase steps ride the stream as `debug` for tracing only. All share the
    `suggest_places.*` prefix so tooling can pick them up by tool name.
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
    """Pack the tool's result + collected reasoning steps into one Command.

    All reasoning steps emitted during the tool's run flow back through
    a single `update` so order is preserved on the receiving end. The
    `messages` slot carries the JSON-encoded `ConsultResult` as the tool
    reply the agent will read on its next tick.
    """
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


def build_suggest_places_tool(
    namer: CandidateNamerService,
    places_search_factory: SearchServiceFactory,
    areas: AreaSuggestionService,
) -> BaseTool:
    """Factory — bind the request-scoped services into the tool.

    `places_search_factory` is the same per-task `PlacesSearchService`
    factory the extraction pipeline uses (ADR-072). The fan-out below
    runs N `find()` calls concurrently and a SQLAlchemy `AsyncSession`
    is not concurrency-safe, so each call opens its own session via the
    factory (cache + provider client are process-safe and shared).
    Sharing a single request-scoped session would deadlock on asyncpg.

    `namer` wraps the process-wide Instructor client and is safe to
    share, but is accepted as an explicit arg so the factory remains
    the single seam for the tool's collaborators.
    """

    @tool(_TOOL_NAME)
    async def suggest_places(
        query: Annotated[str, Field(description=QUERY_DESC)],
        tool_call_id: Annotated[str, InjectedToolCallId],
        state: Annotated[AgentState, InjectedState],
        place_names: Annotated[
            list[str] | None, Field(description=PLACE_NAMES_DESC)
        ] = None,
        categories: Annotated[
            list[PlaceCategory] | None, Field(description=CATEGORIES_DESC)
        ] = None,
        tags: Annotated[list[str] | None, Field(description=TAGS_DESC)] = None,
        neighborhood: Annotated[
            str | None, Field(description=NEIGHBORHOOD_DESC)
        ] = None,
        city: Annotated[str | None, Field(description=CITY_DESC)] = None,
        country: Annotated[str | None, Field(description=COUNTRY_DESC)] = None,
        area_keys: Annotated[
            list[str] | None, Field(description=AREA_KEYS_DESC)
        ] = None,
        limit: Annotated[int | None, Field(description=LIMIT_DESC)] = None,
    ) -> Command[Any]:
        """Propose well-known places, at named areas or the working location."""
        cfg = get_config().agent
        timeout_s = cfg.tool_timeouts_seconds.suggest_places
        default_limit = cfg.suggest_places.default_limit
        max_limit = cfg.suggest_places.max_limit
        effective_limit = max(1, min(limit or default_limit, max_limit))
        name_count = cfg.suggest_places.name_count
        concurrency = cfg.suggest_places.provider_concurrency

        return await with_timeout(
            tool_name=_TOOL_NAME,
            tool_call_id=tool_call_id,
            seconds=timeout_s,
            state=state,
            coro=_run_suggest_places(
                namer=namer,
                places_search_factory=places_search_factory,
                areas=areas,
                state=state,
                tool_call_id=tool_call_id,
                query=query,
                place_names=place_names,
                categories=categories,
                tags=tags,
                neighborhood_override=neighborhood,
                city_override=city,
                country_override=country,
                area_keys=area_keys,
                limit=effective_limit,
                name_count=name_count,
                concurrency=concurrency,
            ),
        )

    return suggest_places


async def _run_suggest_places(
    *,
    namer: CandidateNamerService,
    places_search_factory: SearchServiceFactory,
    areas: AreaSuggestionService,
    state: AgentState,
    tool_call_id: str,
    query: str,
    categories: list[PlaceCategory] | None,
    tags: list[str] | None,
    neighborhood_override: str | None,
    city_override: str | None,
    country_override: str | None,
    limit: int,
    name_count: int,
    concurrency: int,
    # Defaulted so the many existing call sites (and tests) that predate
    # agent-supplied naming keep the namer path unchanged.
    place_names: list[str] | None = None,
    area_keys: list[str] | None = None,
) -> Command[Any]:
    """Inner body — runs the namer + provider phases. Wrapped by with_timeout."""
    with set_tool(_TOOL_NAME):
        return await _run_suggest_places_impl(
            namer=namer,
            places_search_factory=places_search_factory,
            areas=areas,
            state=state,
            tool_call_id=tool_call_id,
            query=query,
            place_names=place_names,
            categories=categories,
            tags=tags,
            neighborhood_override=neighborhood_override,
            city_override=city_override,
            country_override=country_override,
            area_keys=area_keys,
            limit=limit,
            name_count=name_count,
            concurrency=concurrency,
        )


async def _run_suggest_places_impl(
    *,
    namer: CandidateNamerService,
    places_search_factory: SearchServiceFactory,
    areas: AreaSuggestionService,
    state: AgentState,
    tool_call_id: str,
    query: str,
    categories: list[PlaceCategory] | None,
    tags: list[str] | None,
    neighborhood_override: str | None,  # noqa: ARG001 - reserved; see docstring
    city_override: str | None,  # noqa: ARG001 - reserved; see docstring
    country_override: str | None,  # noqa: ARG001 - reserved; see docstring
    limit: int,
    name_count: int,
    concurrency: int,
    place_names: list[str] | None = None,
    area_keys: list[str] | None = None,
) -> Command[Any]:
    """The agent-supplied area overrides (neighborhood / city / country)
    are accepted to keep the arg schema byte-identical to `find_saved`,
    but `suggest_places` enforces a strict lat/lng + radius anchor —
    overrides without coordinates don't bypass that gate. They are
    surfaced in reasoning steps when present and used by the prompt's
    location block once the gate passes.
    """
    steps: list[ReasoningStep] = []
    user_id = state["user_id"]
    base_id = tool_step_base_id(_TOOL_NAME, state)

    # One user-visible row for the whole call (ADR-103): a single skeleton now,
    # filled with the outcome by `_finish` below. The skeleton spans the namer
    # and provider latency, so no per-phase user steps. Internal narration
    # (locate, brainstorm) rides as debug for tracing.
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

    # Area-anchored turn (ADR-140): the agent put areas forward this turn, so
    # the search happens AT those areas rather than around the turn's origin.
    #
    # Anchoring is NOT conditional on the agent also supplying `place_names`.
    # It was, and that made the whole feature opt-out by omission: a turn that
    # verified Hoi An and Hue then asked for suggestions without names fell
    # back to the origin disc and returned somewhere in Da Nang — the exact
    # failure this step exists to fix, reached by forgetting one argument.
    # With no names the namer fills them in as usual; what changes is where
    # they are validated and how they are attributed.
    anchors = anchors_from_state(state, area_keys)
    named = [n for n in (place_names or []) if n.strip()]
    if anchors:
        return await _suggest_at_areas(
            places_search_factory=places_search_factory,
            areas=areas,
            state=state,
            tool_call_id=tool_call_id,
            anchors=capped(anchors, get_config().agent.area_anchor.max_areas),
            place_names=named,
            namer=namer,
            query=query,
            categories=categories,
            taste_summary=state.get("taste_profile_summary") or "",
            name_count=name_count,
            tags=tags,
            limit=limit,
            concurrency=concurrency,
            movement_cfg=movement_cfg,
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

    assert working is not None  # narrowed by is_anchored
    # Utility errands ("ATM near me") are walked to — clamp to a walkable
    # radius so the namer scope and provider locationBias stay tight and the
    # nearest branch wins, not a prominent one across town.
    working = clamp_to_walkable_for_utility(working, categories, movement_cfg)

    # Route-shaped turn (ADR-136). The length gate runs before the namer so an
    # all-oversized route ("Hanoi to Saigon") costs nothing at all: no LLM
    # call, no provider call, and an honest city-scale answer instead of five
    # unrelated venues strung across a country.
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
        _trace("locate", f"looking along {route_summary(working)}")
    else:
        _trace("locate", f"looking around {_location_label(working)}")

    # The agent may supply the names itself when it already knows the area
    # (ADR-137). It is the strongest model in the turn, so its own knowledge of
    # what is worth going to beats a helper model's — and validation is
    # identical either way, so a name it invents is dropped exactly like one
    # the namer invents. The namer stays as the fallback for when the agent
    # has nothing specific in mind.
    supplied = [name.strip() for name in (place_names or []) if name.strip()]
    if supplied:
        proposed = [
            # The agent writes the user-facing rationale in its own prose, so
            # the per-candidate reason stays empty here rather than being
            # invented by the tool layer.
            CandidateName(name=name, reason="", icon=None)
            for name in supplied
        ]
        _trace("agent_names", f"checking {len(proposed)} you had in mind")
    else:
        namer_result = await namer.generate(
            intent=query,
            working=working,
            categories=categories,
            tags=tags,
            taste_summary=state.get("taste_profile_summary") or "",
            count=name_count,
            user_id=user_id,
        )
        proposed = namer_result.candidates

    if not proposed:
        _finish("nothing specific came to mind here", kind="namer_empty")
        return _build_command(
            state=state,
            tool_call_id=tool_call_id,
            result=ConsultResult(candidates=[], empty_reason="no_match"),
            steps=steps,
        )

    preview = ", ".join(c.name for c in proposed[:2])
    extra = "" if len(proposed) <= 2 else f", +{len(proposed) - 2} more"
    _trace("brainstorm", f"a few ideas — {preview}{extra}")

    # On a route, one wide disc covering the whole path replaces the disc
    # around the origin — so a name anywhere along the way still validates in
    # the SAME one call per name. It is coarse on purpose; `filter_and_order`
    # below is what makes the result actually route-shaped.
    place_loc = (
        enclosing_context(working, movement_cfg)
        if on_route
        else _build_location_context(working)
    )
    validated = await _validate_candidates(
        places_search_factory=places_search_factory,
        proposed=proposed,
        location=place_loc,
        concurrency=concurrency,
    )
    if on_route:
        validated = filter_and_order(
            validated,
            working,
            movement_cfg,
            coords=lambda pair: place_coords(pair[0]),
        )
    if not validated:
        _finish(
            NOTHING_ON_ROUTE if on_route else "none of those turned up near you",
            kind="no_provider_hits",
        )
        return _build_command(
            state=state,
            tool_call_id=tool_call_id,
            result=ConsultResult(candidates=[], empty_reason="no_match"),
            steps=steps,
        )

    # Drop anything another tool already returned this turn — the user's own
    # save has already been shown as theirs, and showing it again as a fresh
    # suggestion is one place rendered as two cards.
    surfaced = _already_surfaced_ids(state)
    if surfaced:
        validated = [
            pair for pair in validated if (pair[0].provider_id or "") not in surfaced
        ]

    # Safety values (dietary/accessibility) exclude; other tag values are
    # preference signals — the FULL tag list already steered the namer's
    # name proposals and the validation query, so its work is done. A
    # fresh place with no experiential tags yet must not be zeroed out
    # (ADR-118).
    hard, _soft = split_constraints(tags or [])
    filtered: list[tuple[PlaceCore, str]] = []
    for place, reason in validated:
        if hard_constraints_satisfied(place, hard):
            filtered.append((place, reason))
    dropped = len(validated) - len(filtered)

    if not filtered:
        _finish(NONE_FIT, kind="constraints_drop")
        return _build_command(
            state=state,
            tool_call_id=tool_call_id,
            result=ConsultResult(candidates=[], empty_reason="no_match"),
            steps=steps,
        )

    final = filtered[:limit]
    final_names = [place.place_name for place, _ in final]
    _finish(found_summary(final_names, dropped=dropped if hard else 0))

    candidates = await _to_candidates(
        [(place, reason, None) for place, reason in final], areas
    )

    return _build_command(
        state=state,
        tool_call_id=tool_call_id,
        result=ConsultResult(candidates=candidates, empty_reason=None),
        steps=steps,
    )


async def _name_across_areas(
    *,
    namer: CandidateNamerService,
    anchors: list[AreaEntity],
    query: str,
    categories: list[PlaceCategory] | None,
    tags: list[str] | None,
    taste_summary: str,
    name_count: int,
    user_id: str,
) -> list[CandidateName]:
    """Ask the namer for ideas **in the named areas**, one call, not N.

    The namer takes a single location, so the areas are folded into the intent
    text instead of firing one LLM call per area — the fan-out this step caps
    is the provider's, and multiplying the model calls to match would be the
    wrong trade. The first area supplies the location block so mobility and
    density scaling stay coherent.

    Anchored on the areas rather than the turn's origin: asked about Hoi An
    and Hue from a phone sitting in Da Nang, the origin-anchored namer returns
    Da Nang.
    """
    area_names = ", ".join(a.name for a in anchors)
    lead = anchors[0]
    working = WorkingLocation(
        country=lead.country_code,
        city=lead.name,
        lat=lead.lat,
        lng=lead.lng,
        country_code=lead.country_code,
        search_radius_m=anchor_radius_m(lead, get_config().movement),
    )
    result = await namer.generate(
        intent=f"{query} — in {area_names}",
        working=working,
        categories=categories,
        tags=tags,
        taste_summary=taste_summary,
        count=name_count,
        user_id=user_id,
    )
    return result.candidates


async def _suggest_at_areas(
    *,
    places_search_factory: SearchServiceFactory,
    areas: AreaSuggestionService,
    state: AgentState,
    tool_call_id: str,
    anchors: list[AreaEntity],
    place_names: list[str],
    namer: CandidateNamerService,
    query: str,
    categories: list[PlaceCategory] | None,
    taste_summary: str,
    name_count: int,
    tags: list[str] | None,
    limit: int,
    concurrency: int,
    movement_cfg: MovementConfig,
    steps: list[ReasoningStep],
    finish: Callable[..., None],
    trace: Callable[[str, str], None],
) -> Command[Any]:
    """Find places inside the areas the agent put forward.

    Structurally identical to the corridor path, for the same reason: one
    coarse disc covers every anchored area so the provider call count stays
    one-per-name, and precision comes afterwards from `attribute_to_area`.
    A validated place that sits in none of the named areas is dropped — that
    drop is the point, because a name that resolves to a same-named place two
    cities away is exactly what the loose disc lets in.

    Names come from the agent when it has them (ADR-140 — its knowledge of
    what is worth going to beats a helper model's) and from the namer
    otherwise. The namer is asked *about the named areas* rather than about
    the turn's origin, which is the difference between "somewhere in Hoi An"
    and "somewhere near where the user happens to be standing".
    """
    names = [n.strip() for n in place_names if n.strip()]
    if names:
        trace("areas", f"checking {len(names)} places across {len(anchors)} areas")
        proposed = [CandidateName(name=name, reason="", icon=None) for name in names]
    else:
        trace("areas", f"looking for ideas across {len(anchors)} areas")
        proposed = await _name_across_areas(
            namer=namer,
            anchors=anchors,
            query=query,
            categories=categories,
            tags=tags,
            taste_summary=taste_summary,
            name_count=name_count,
            user_id=state["user_id"],
        )
        if not proposed:
            finish("nothing specific came to mind there", kind="namer_empty")
            return _build_command(
                state=state,
                tool_call_id=tool_call_id,
                result=ConsultResult(candidates=[], empty_reason="no_match"),
                steps=steps,
            )

    validated = await _validate_candidates(
        places_search_factory=places_search_factory,
        proposed=proposed,
        location=enclosing_anchor_context(anchors, movement_cfg),
        concurrency=concurrency,
    )

    placed: list[tuple[PlaceCore, str, AreaEntity]] = []
    for place, reason in validated:
        point = place_coords(place)
        if point is None:
            continue
        area = attribute_to_area(anchors, movement_cfg, point[0], point[1])
        if area is not None:
            placed.append((place, reason, area))

    surfaced = _already_surfaced_ids(state)
    if surfaced:
        placed = [
            item for item in placed if (item[0].provider_id or "") not in surfaced
        ]

    hard, _soft = split_constraints(tags or [])
    kept = [item for item in placed if hard_constraints_satisfied(item[0], hard)]
    dropped = len(placed) - len(kept)

    if not kept:
        finish(
            NONE_FIT if dropped else "none of those turned up in those areas",
            kind="constraints_drop" if dropped else "no_provider_hits",
        )
        return _build_command(
            state=state,
            tool_call_id=tool_call_id,
            result=ConsultResult(candidates=[], empty_reason="no_match"),
            steps=steps,
        )

    final = kept[:limit]
    finish(
        found_summary(
            [place.place_name for place, _, _ in final],
            dropped=dropped if hard else 0,
        )
    )
    return _build_command(
        state=state,
        tool_call_id=tool_call_id,
        result=ConsultResult(
            candidates=await _to_candidates(
                [(place, reason, area.entity_key) for place, reason, area in final],
                areas,
            ),
            empty_reason=None,
        ),
        steps=steps,
    )


async def _to_candidates(
    placed: list[tuple[PlaceCore, str, str | None]],
    areas: AreaSuggestionService,
) -> list[ConsultCandidate]:
    """Turn validated places into candidates, correcting kind where needed.

    A name kebi already knows to be an area comes back as an **area**
    candidate rather than a venue one — Hai Van Pass is geography whichever
    provider record answered for it, and a venue card carries a save action
    that would write it into the library as a restaurant-shaped row.

    This is a correction, not a guard: the place still appears in the answer,
    with its extent instead of a pin. Blocking it outright would also block
    Lang Co Beach, which the provider types identically and which is one of
    the best stops on the same road.

    Best-effort — a failed lookup leaves every candidate a venue, which is
    exactly today's behaviour, so the store being unreachable degrades the
    kind rather than the answer.
    """
    try:
        known = await areas.known_areas([place.place_name for place, _, _ in placed])
    except Exception as exc:  # noqa: BLE001 - kind correction never fails a turn
        logger.warning("area kind check failed: %s", exc)
        known = {}

    candidates: list[ConsultCandidate] = []
    for place, reason, anchor_key in placed:
        area = known.get(place.place_name)
        if area is not None:
            candidates.append(ConsultCandidate.for_area(area, reason=reason or None))
            continue
        candidates.append(
            ConsultCandidate(
                place=place,
                user_data=None,
                source="suggested",
                rrf_score=0.0,
                # Empty when the agent named this itself — it writes the
                # rationale in its own prose, and an empty string would render
                # as a blank reason line rather than no reason at all.
                reason=reason or None,
                anchor_area_key=anchor_key,
            )
        )
    return candidates


def _already_surfaced_ids(state: AgentState) -> set[str]:
    """Provider ids another tool has already returned this turn.

    The consult tools each dedupe internally but not against each other, so a
    place the user has saved comes back from `find_saved` AND again from
    `suggest_places` — two cards for one place, under two different
    `recommendation_id`s, which also means a later accept/save signal could
    attribute to the wrong recommendation.

    Tools run one at a time (`parallel_tool_calls=False`), so by the time this
    runs the earlier `ToolMessage` is already in `messages`. The saved copy
    wins because it carries `user_data` — "you've been meaning to go here" is
    a better card than "here's a suggestion".
    """
    seen: set[str] = set()
    for message in state.get("messages") or []:
        if not isinstance(message, ToolMessage):
            continue
        try:
            result = ConsultResult.model_validate_json(str(message.content))
        except Exception:  # noqa: BLE001 - a non-consult tool payload, skip it
            continue
        for candidate in result.candidates:
            if candidate.place is not None and candidate.place.provider_id:
                seen.add(candidate.place.provider_id)
    return seen


# How many provider results to consider per proposed name. Google bills the
# request, not the result count (the field mask is what sets the tier), so
# asking for a few costs the same as asking for one and gives the name match
# something to choose between.
_NAME_MATCH_CANDIDATES = 5


def _best_name_match(asked: str, hits: list[PlaceObject]) -> PlaceObject | None:
    """Pick the hit that best answers the name that was asked for.

    Nearest-first ordering is right for a brand ("the closest Watsons") and
    wrong for a landmark: asking for "Tam Coc" returned *Tam Coc Homestay Of
    Ms Loan* because a guesthouse happened to sit closer to the search centre
    than the karst boat ride, and "Marble Mountains" returned the *Ticket
    Booth (Gate A1)* rather than the mountains. Both correct places were in
    the results; distance simply outranked them.

    So: an exact name match wins outright, then a name that merely wraps the
    asked-for one (fewest extra words first, so "The Marble Mountains" beats
    "Marble Mountains Ticket Booth (Gate A1)"), and failing both, the
    provider's own nearest-first order stands.

    Deliberately a preference, not a filter — falling through to the first hit
    keeps every result today's behaviour would have kept. A strict name filter
    would reject "Bach Ma National Park" → *Vườn Quốc Gia Bạch Mã*, which is
    the same place under its Vietnamese name.
    """
    if not hits:
        return None

    asked_slug = _slugify(asked)
    if not asked_slug:
        return hits[0]

    exact: PlaceObject | None = None
    wrapping: tuple[int, int, PlaceObject] | None = None
    for hit in hits:
        hit_slug = _slugify(hit.place_name)
        if not hit_slug:
            continue
        if hit_slug == asked_slug:
            exact = exact or hit
            continue
        if asked_slug in hit_slug:
            # Rank by how much the provider's name adds beyond what was
            # asked: extra words first, then raw length as the tie-break.
            # "The Marble Mountains" and "Marble Mountains Elevator" both add
            # one word, and the shorter is the mountains themselves rather
            # than a facility at them.
            extra = len(hit_slug.split("-")) - len(asked_slug.split("-"))
            rank = (extra, len(hit_slug))
            if wrapping is None or rank < (wrapping[0], wrapping[1]):
                wrapping = (extra, len(hit_slug), hit)

    if exact is not None:
        return exact
    if wrapping is not None:
        return wrapping[2]
    return hits[0]


async def _validate_candidates(
    *,
    places_search_factory: SearchServiceFactory,
    proposed: list[CandidateName],
    location: LocationContext,
    concurrency: int,
) -> list[tuple[PlaceCore, str]]:
    """Fan out proposed names to the place provider, return validated pairs.

    For each proposed name, run a single bounded `PlacesSearchService.find()`
    call carrying the working location's `locationBias.circle`, ordered
    nearest-first (`sort_by="distance"`). The nearest-first ordering matters
    for brand/chain names — a chain resolves to many branches, and without
    it the provider's relevance rank returns the most prominent (often a
    far flagship) rather than the closest branch. Names with no provider
    hit are dropped (the location bias drops out-of-radius results, so this
    is also how "famous spots in another city" self-eliminate). On the way
    back the live `PlaceObject` is
    stripped to a persistable `PlaceCore` — the agent surface only needs
    canonical fields, not the cache-only live half.

    Concurrency is bounded by `provider_concurrency` so a noisy namer
    output can't overrun Google's quota or the session pool. **Each
    parallel lookup opens its own `PlacesSearchService` via the factory
    so each gets its own `AsyncSession`** — SQLAlchemy / asyncpg
    sessions are not concurrency-safe, and the extraction pipeline
    already established this pattern (`_extend_search_set`). The cache
    and provider client are process-safe and shared across the calls.

    Order is preserved (zipped through `asyncio.gather` — the namer's
    order is the user-facing order). Pairs the validated `PlaceCore`
    with the namer's `reason` for the candidate so it survives into
    the result.
    """
    if not proposed:
        return []

    # Dedup proposed names before the provider fan-out. The namer can emit the
    # same place under two phrasings ("Wat Pho" / "Wat Pho Temple"), and each
    # would otherwise fire its own billed provider lookup. Collapse on the
    # normalized name (same key the extraction pipeline dedups on), keeping the
    # first occurrence so the namer's preferred phrasing + reason survive. The
    # post-fetch provider_id dedup below still catches distinct names that
    # resolve to the same place; this only removes redundant *calls*.
    deduped: list[CandidateName] = []
    seen_names: set[str] = set()
    for candidate in proposed:
        key = normalize_query(candidate.name)
        if not key or key in seen_names:
            continue
        seen_names.add(key)
        deduped.append(candidate)
    proposed = deduped

    sem = asyncio.Semaphore(concurrency)

    async def _lookup(candidate: CandidateName) -> PlaceObject | None:
        try:
            async with sem, places_search_factory() as svc:
                hits = await svc.find(
                    PlaceQuery(
                        place_names=[candidate.name],
                        location=location,
                        # Brand names (a chain) resolve to many branches;
                        # order nearest-first so limit=1 returns the closest
                        # branch, not the most prominent/flagship one.
                        sort_by="distance",
                        # The namer's icon rides the query so the cold-path
                        # write-through persists it with the row's one
                        # normal upsert (ADR-117).
                        icon_hint=candidate.icon,
                    ),
                    limit=_NAME_MATCH_CANDIDATES,
                )
        except Exception as exc:
            logger.warning(
                "suggest_places provider lookup failed for %r: %s",
                candidate.name,
                exc,
                exc_info=True,
            )
            return None
        return _best_name_match(candidate.name, hits)

    hit_objs = await asyncio.gather(*(_lookup(c) for c in proposed))
    results: list[tuple[PlaceCore, str]] = []
    seen_ids: set[str] = set()
    for candidate, hit in zip(proposed, hit_objs, strict=True):
        if hit is None:
            continue
        core = hit.to_core()
        dedup_key = core.provider_id or core.id or core.place_name
        if dedup_key in seen_ids:
            continue
        seen_ids.add(dedup_key)
        icon = normalize_icon(candidate.icon)
        if core.icon is None and icon is not None:
            # Warm-path row (pre-dated this turn) — the icon_hint only
            # persists on the cold-path write-through, so stamp the
            # response copy for display; the DB row keeps NULL.
            core = core.model_copy(update={"icon": icon})
        results.append((core, candidate.reason))
    return results
