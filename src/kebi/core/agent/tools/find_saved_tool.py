"""`find_saved` agent tool — search the user's own saved places.

First tool re-introduced after ADR-075. Reads from the user's saved
collection only via the existing `HybridSearchService` (ADR-070), with
the existing pgvector + FTS + RRF score as the ordering. Filters are
agent-prepared: the LLM is responsible for translating user intent and
memory context into the structured filter args. The tool itself never
calls an LLM and never reaches an external place provider.

Geofence is taken from the per-turn `WorkingLocation` on `AgentState`
(ADR-083/084). If the agent names a specific area (neighborhood / city
/ country) the geofence is dropped for that call — "places in Chiang
Mai" should not also require "within 2km of Bangkok".

The shape of this tool — one structured payload, no LLM-supplied
location coordinates, single `ConsultResult` JSON output — is the
template the other place tools follow.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from pydantic import Field

from kebi.core.agent._trace_context import set_tool
from kebi.core.agent.location import WorkingLocation
from kebi.core.agent.reasoning import ReasoningStep
from kebi.core.agent.state import AgentState
from kebi.core.agent.stream_emit import emit_step_active, emit_step_done
from kebi.core.agent.tools._hard_constraints import split_constraints
from kebi.core.agent.tools._notes import attach_notes
from kebi.core.agent.tools._packing import pack_consult_result
from kebi.core.agent.tools._scope import anchor_to_corridor, itinerary_segments
from kebi.core.agent.tools._search_args import (
    CATEGORIES_DESC,
    CITY_DESC,
    COUNTRY_DESC,
    LIMIT_DESC,
    NEIGHBORHOOD_DESC,
    QUERY_DESC,
    TAGS_DESC,
    CategoryArg,
)
from kebi.core.agent.tools._summaries import NEED_LOCATION, TITLES, found_summary
from kebi.core.agent.tools._with_timeout import tool_step_base_id, with_timeout
from kebi.core.agent.tools._working_location import maybe_working_location
from kebi.core.agent.tools.consult_models import ConsultCandidate, ConsultResult
from kebi.core.config import get_config
from kebi.core.knowledge.candidate_notes_service import CandidateNotesService
from kebi.core.places.hybrid_search_service import HybridSearchService
from kebi.core.places.models import HybridSearchFilters, PlaceCategory

logger = logging.getLogger(__name__)


_TOOL_NAME = "find_saved"


def _eq_ci(a: str | None, b: str | None) -> bool:
    """Case-insensitive equality, ignoring whitespace and treating None as no-match."""
    if not a or not b:
        return False
    return a.strip().casefold() == b.strip().casefold()


def _assemble_filters(
    *,
    categories: list[PlaceCategory] | None,
    tags: list[str] | None,
    neighborhood: str | None,
    city: str | None,
    country: str | None,
    working: WorkingLocation | None,
) -> HybridSearchFilters:
    """Build the HybridSearchFilters payload sent to the search service.

    Named-area args from the agent (neighborhood / city / country) win
    over the WorkingLocation-derived geofence. With no named area, fall
    back to lat/lng + the scope-derived radius computed at resolve time.

    **Redundant named-area stripping.** When an agent-supplied area
    matches the working_location's own value (case-insensitive), it
    is treated as no-op and dropped — the geofence already covers it.
    Without this, an agent that defensively passed `city="Koh Samui"`
    on a Koh-Samui-working-location turn would (a) suppress the
    geofence and (b) match `city ILIKE '%Koh Samui%'` against
    `places.city`, which is NULL for many Google-derived rows in
    rural/island Thailand — yielding zero results despite real saves
    on the island. After stripping, the turn uses the geofence and
    the saves surface.

    **Soft tags never filter (ADR-137).** `HybridSearchFilters.tags`
    is an AND predicate on persisted rows, so passing the agent's tags
    through wholesale meant one vibe or time value — `lively`, `night`,
    `serves_cocktails` — could zero out a user's entire saved
    collection, because a saved row only carries the tags extraction
    happened to give it. That is the single biggest reason saves failed
    to show up in answers. Only the safety classes (dietary,
    accessibility) filter here, matching what `suggest_places` and
    `suggest_places` already does via `split_constraints`; the rest ride
    the query text, where they steer the vector and FTS legs without
    excluding anything.
    """
    eff_neighborhood = (
        None
        if (working is not None and _eq_ci(neighborhood, working.neighborhood))
        else neighborhood
    )
    eff_city = None if (working is not None and _eq_ci(city, working.city)) else city
    eff_country = (
        None if (working is not None and _eq_ci(country, working.country)) else country
    )

    has_named_area = bool(eff_neighborhood or eff_city or eff_country)
    if has_named_area or working is None:
        lat: float | None = None
        lng: float | None = None
        radius_m: int | None = None
    else:
        # search_radius_m is a float in WorkingLocation but HybridSearchFilters
        # types it int; round defensively and clamp to a positive value. lat
        # and lng must move with radius — HybridSearchFilters rejects a point
        # without a radius — so drop the trio together when the resolver
        # didn't supply one.
        if working.search_radius_m > 0:
            lat = working.lat
            lng = working.lng
            radius_m = int(working.search_radius_m)
        else:
            lat = None
            lng = None
            radius_m = None

    hard_tags, _soft_tags = split_constraints(tags or [])
    return HybridSearchFilters(
        categories=categories or None,
        tags=hard_tags or None,
        neighborhood=eff_neighborhood or None,
        city=eff_city or None,
        country=eff_country or None,
        lat=lat,
        lng=lng,
        radius_m=radius_m,
    )


def _query_with_soft_tags(query: str, tags: list[str] | None) -> str:
    """Fold the non-safety tag values into the retrieval query text.

    Values already present in the query are not repeated — the agent is told
    not to duplicate constraint values into `query`, but this is cheap
    insurance against double-weighting when it does.
    """
    _hard, soft = split_constraints(tags or [])
    lowered = query.lower()
    extra = [t for t in soft if t.strip() and t.strip().lower() not in lowered]
    if not extra:
        return query
    return f"{query} {' '.join(extra)}".strip()


def _summarise(result: ConsultResult) -> str:
    """One-line user-visible step summary.

    Plain narration, no tool name or raw query echo (per project feedback on
    user-facing reasoning steps). The success branch surfaces the matched
    place names (capped to a short preview) so the user sees what was found
    without parsing the structured `tool_results` payload — shares the
    `found_summary` register with the other place tools.
    """
    if result.empty_reason == "no_saves":
        return "no saved places yet"
    if result.empty_reason == "no_match":
        return "nothing saved matched that"
    if result.empty_reason == "no_location":
        return NEED_LOCATION

    names = [c.place.place_name for c in result.candidates]
    return found_summary(names)


def build_find_saved_tool(
    hybrid_search: HybridSearchService,
    notes_service: CandidateNotesService | None = None,
) -> BaseTool:
    """Factory — bind the request-scoped HybridSearchService into the tool.

    Per-request DI: hybrid_search closes over a request-scoped DB session
    (ADR-072 — services that hold sessions are not SSP-cacheable).
    """

    @tool(_TOOL_NAME)
    async def find_saved(
        query: Annotated[str, Field(description=QUERY_DESC)],
        tool_call_id: Annotated[str, InjectedToolCallId],
        state: Annotated[AgentState, InjectedState],
        categories: Annotated[CategoryArg, Field(description=CATEGORIES_DESC)] = None,
        tags: Annotated[list[str] | None, Field(description=TAGS_DESC)] = None,
        neighborhood: Annotated[
            str | None, Field(description=NEIGHBORHOOD_DESC)
        ] = None,
        city: Annotated[str | None, Field(description=CITY_DESC)] = None,
        country: Annotated[str | None, Field(description=COUNTRY_DESC)] = None,
        limit: Annotated[int | None, Field(description=LIMIT_DESC)] = None,
    ) -> Command[Any]:
        """Search the user's saved places by intent, filters, and turn location."""
        cfg = get_config().agent
        timeout_s = cfg.tool_timeouts_seconds.find_saved
        default_limit = cfg.find_saved.default_limit
        max_limit = cfg.find_saved.max_limit
        effective_limit = max(1, min(limit or default_limit, max_limit))

        return await with_timeout(
            tool_name=_TOOL_NAME,
            tool_call_id=tool_call_id,
            seconds=timeout_s,
            state=state,
            coro=_run_find_saved(
                hybrid_search=hybrid_search,
                notes_service=notes_service,
                state=state,
                tool_call_id=tool_call_id,
                query=query,
                categories=categories,
                tags=tags,
                neighborhood=neighborhood,
                city=city,
                country=country,
                limit=effective_limit,
            ),
        )

    return find_saved


async def _run_find_saved(
    *,
    hybrid_search: HybridSearchService,
    notes_service: CandidateNotesService | None = None,
    state: AgentState,
    tool_call_id: str,
    query: str,
    categories: list[PlaceCategory] | None,
    tags: list[str] | None,
    neighborhood: str | None,
    city: str | None,
    country: str | None,
    limit: int,
) -> Command[Any]:
    """Inner body — runs the search, packs the result. Wrapped by with_timeout."""
    with set_tool(_TOOL_NAME):
        return await _run_find_saved_impl(
            hybrid_search=hybrid_search,
            notes_service=notes_service,
            state=state,
            tool_call_id=tool_call_id,
            query=query,
            categories=categories,
            tags=tags,
            neighborhood=neighborhood,
            city=city,
            country=country,
            limit=limit,
        )


async def _run_find_saved_impl(
    *,
    hybrid_search: HybridSearchService,
    notes_service: CandidateNotesService | None = None,
    state: AgentState,
    tool_call_id: str,
    query: str,
    categories: list[PlaceCategory] | None,
    tags: list[str] | None,
    neighborhood: str | None,
    city: str | None,
    country: str | None,
    limit: int,
) -> Command[Any]:
    user_id = state["user_id"]
    # SSE lifecycle: announce the step before the search latency.
    base_id = tool_step_base_id(_TOOL_NAME, state)
    started = emit_step_active(
        base_id, _TOOL_NAME, title=TITLES[_TOOL_NAME], source="agent"
    )

    working = maybe_working_location(state)
    if working is not None:
        # A saved place on the way counts as on the way (ADR-137) — without
        # this the geofence is a circle around where the trip starts.
        working = anchor_to_corridor(working)
    has_named_area = bool(neighborhood or city or country)

    # A multi-stop trip fans one call out across every stop and leg
    # (ADR-148) — one search each, results labelled with the segment they
    # came from, deduped in segment order so a place in a stop's own disc
    # is that stop's, and only a hit outside every stop stays "on the way".
    # An agent-named area wins over the fan-out, same as it wins over the
    # geofence: "what did I save in Hue" is one city, not the whole trip.
    segments = (
        itinerary_segments(working)
        if (working is not None and not has_named_area)
        else []
    )
    if segments:
        per_segment = get_config().agent.itinerary.per_segment_limit
        effective_query = _query_with_soft_tags(query, tags)
        seen: set[str] = set()
        candidates: list[ConsultCandidate] = []
        for segment in segments:
            seg_hits = await hybrid_search.search(
                user_id=user_id,
                query=effective_query,
                filters=_assemble_filters(
                    categories=categories,
                    tags=tags,
                    neighborhood=None,
                    city=None,
                    country=None,
                    working=segment.working,
                ),
                limit=per_segment,
            )
            for hit in seg_hits:
                key = hit.place.id or hit.place.place_name
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    ConsultCandidate(
                        place=hit.place,
                        user_data=hit.user_data,
                        source="saved",
                        rrf_score=hit.rrf_score,
                        vector_rank=hit.vector_rank,
                        text_rank=hit.text_rank,
                        segment=segment.label,
                    )
                )
    else:
        filters = _assemble_filters(
            categories=categories,
            tags=tags,
            neighborhood=neighborhood,
            city=city,
            country=country,
            working=working,
        )

        hits = await hybrid_search.search(
            user_id=user_id,
            # Soft tags no longer filter (see `_assemble_filters`), so they
            # ride the query text — still steering the vector + FTS legs,
            # never excluding a save that simply hasn't been tagged that way
            # yet.
            query=_query_with_soft_tags(query, tags),
            filters=filters,
            limit=limit,
        )

        candidates = [
            ConsultCandidate(
                place=hit.place,
                user_data=hit.user_data,
                source="saved",
                rrf_score=hit.rrf_score,
                vector_rank=hit.vector_rank,
                text_rank=hit.text_rank,
            )
            for hit in hits
        ]

    empty_reason: str | None = None
    if not candidates:
        if working is None and not has_named_area:
            empty_reason = "no_location"
        else:
            empty_reason = "no_match"

    result = await attach_notes(
        ConsultResult(candidates=candidates, empty_reason=empty_reason),
        notes_service=notes_service,
        user_id=user_id,
        working=working,
    )

    step = ReasoningStep(
        step=f"{_TOOL_NAME}.summary",
        title=TITLES[_TOOL_NAME],
        summary=_summarise(result),
        source="agent",
        visibility="user",
        duration_ms=0.0,
    )
    emit_step_done(base_id, step, started=started)

    return Command(
        update=pack_consult_result(
            state=state,
            tool_name=_TOOL_NAME,
            tool_call_id=tool_call_id,
            result=result,
            extra={
                "reasoning_steps": (state.get("reasoning_steps") or []) + [step],
                "tool_calls_used": state.get("tool_calls_used", 0) + 1,
            },
        )
    )
