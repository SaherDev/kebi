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
from kebi.core.agent.tools.candidate_namer import (
    CandidateName,
    CandidateNamerService,
)
from kebi.core.agent.tools.consult_models import ConsultCandidate, ConsultResult
from kebi.core.config import get_config
from kebi.core.extraction.candidate_mapper import normalize_query
from kebi.core.extraction.extraction_pipeline import SearchServiceFactory
from kebi.core.places.models import (
    LocationContext,
    PlaceCategory,
    PlaceCore,
    PlaceObject,
    PlaceQuery,
)

logger = logging.getLogger(__name__)

_TOOL_NAME = "suggest_places"


def _maybe_working_location(state: AgentState) -> WorkingLocation | None:
    """Read the turn's working location off state, returning None on absence.

    Mirrors the helper in `find_saved_tool.py`. Stays local rather than
    shared because the two tools may diverge on what they accept as
    "usable" working_location — `suggest_places` additionally requires
    a non-zero search radius (see `_is_anchored`).
    """
    wl_dict = state.get("working_location")
    if not wl_dict:
        return None
    try:
        return WorkingLocation.model_validate(wl_dict)
    except Exception:
        logger.warning("working_location on state failed validation; ignoring")
        return None


def _is_anchored(working: WorkingLocation | None) -> bool:
    """Strict location-anchoring gate for the namer + provider phases.

    `suggest_places` does not call the namer LLM or the provider without
    a real geographic anchor. The `find_saved` tool accepts named-area
    overrides (city / country) as a substitute for lat/lng — but the
    namer needs the radius + density to scope its suggestions, and the
    provider's locationBias.circle needs lat/lng + radius_m. A turn
    that lacks either is a `no_location` outcome.
    """
    if working is None:
        return False
    return working.search_radius_m > 0


def _build_location_context(working: WorkingLocation) -> LocationContext:
    """Bound every provider call to the resolved working location.

    Carries lat/lng + radius_m (required by `PlaceQuery._validate_geo_location`)
    plus city/country/neighborhood so the provider's text-search reranker
    has the named context too. `_is_anchored` has already gated this —
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
        categories: Annotated[
            list[PlaceCategory] | None, Field(description=CATEGORIES_DESC)
        ] = None,
        tags: Annotated[list[str] | None, Field(description=TAGS_DESC)] = None,
        neighborhood: Annotated[
            str | None, Field(description=NEIGHBORHOOD_DESC)
        ] = None,
        city: Annotated[str | None, Field(description=CITY_DESC)] = None,
        country: Annotated[str | None, Field(description=COUNTRY_DESC)] = None,
        limit: Annotated[int | None, Field(description=LIMIT_DESC)] = None,
    ) -> Command[Any]:
        """Propose well-known places near the working location, validated."""
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
                state=state,
                tool_call_id=tool_call_id,
                query=query,
                categories=categories,
                tags=tags,
                neighborhood_override=neighborhood,
                city_override=city,
                country_override=country,
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
) -> Command[Any]:
    """Inner body — runs the namer + provider phases. Wrapped by with_timeout."""
    with set_tool(_TOOL_NAME):
        return await _run_suggest_places_impl(
            namer=namer,
            places_search_factory=places_search_factory,
            state=state,
            tool_call_id=tool_call_id,
            query=query,
            categories=categories,
            tags=tags,
            neighborhood_override=neighborhood_override,
            city_override=city_override,
            country_override=country_override,
            limit=limit,
            name_count=name_count,
            concurrency=concurrency,
        )


async def _run_suggest_places_impl(
    *,
    namer: CandidateNamerService,
    places_search_factory: SearchServiceFactory,
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
    # Utility errands ("ATM near me") are walked to — clamp to a walkable
    # radius so the namer scope and provider locationBias stay tight and the
    # nearest branch wins, not a prominent one across town.
    working = clamp_to_walkable_for_utility(working, categories, get_config().movement)
    location_label = _location_label(working)
    _trace("locate", f"looking around {location_label}")

    namer_result = await namer.generate(
        intent=query,
        working=working,
        categories=categories,
        tags=tags,
        taste_summary=state.get("taste_profile_summary") or "",
        count=name_count,
        user_id=user_id,
    )
    proposed: list[CandidateName] = namer_result.candidates
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

    place_loc = _build_location_context(working)
    validated = await _validate_candidates(
        places_search_factory=places_search_factory,
        proposed=proposed,
        location=place_loc,
        concurrency=concurrency,
    )
    if not validated:
        _finish("none of those turned up near you", kind="no_provider_hits")
        return _build_command(
            state=state,
            tool_call_id=tool_call_id,
            result=ConsultResult(candidates=[], empty_reason="no_match"),
            steps=steps,
        )

    filtered: list[tuple[PlaceCore, str]] = []
    for place, reason in validated:
        if hard_constraints_satisfied(place, tags or []):
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
    _finish(found_summary(final_names, dropped=dropped if tags else 0))

    candidates = [
        ConsultCandidate(
            place=place,
            user_data=None,
            source="suggested",
            rrf_score=0.0,
            reason=reason,
        )
        for place, reason in final
    ]

    return _build_command(
        state=state,
        tool_call_id=tool_call_id,
        result=ConsultResult(candidates=candidates, empty_reason=None),
        steps=steps,
    )


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
                    ),
                    limit=1,
                )
        except Exception as exc:
            logger.warning(
                "suggest_places provider lookup failed for %r: %s",
                candidate.name,
                exc,
                exc_info=True,
            )
            return None
        return hits[0] if hits else None

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
        results.append((core, candidate.reason))
    return results
