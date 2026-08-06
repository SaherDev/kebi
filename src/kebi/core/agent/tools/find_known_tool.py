"""`find_known` agent tool — places kebi's own knowledge points to (ADR-138).

The fourth place tool, and the only one whose retrieval key is a *fact*. The
other three ask "which places match this query" and then, since ADR-137, get
kebi's claims attached to whatever they found. This one starts from the claims:
of everything kebi knows about this area, what actually answers the question —
and which places do those facts name?

That ordering is the difference between a generic answer and a local's. "Where
should I go tonight" in Canggu on a Monday is not won by ranking nearby
nightclubs; it is won by knowing Monday is Luigi's night. Attaching that fact
to Luigi's only helps if something already surfaced Luigi's — so this path
exists to surface it.

Free to call: one geofenced claims join plus one catalog read by id. No LLM,
no place provider. That is why the prompt leads with it on a recommendation
turn — it spends nothing and, when it returns anything at all, returns the
highest-signal candidates in the turn.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from pydantic import Field

from kebi.core.agent._trace_context import set_tool
from kebi.core.agent.graph import local_daypart, local_season, local_weekday
from kebi.core.agent.reasoning import ReasoningStep
from kebi.core.agent.state import AgentState
from kebi.core.agent.stream_emit import emit_step_active, emit_step_done
from kebi.core.agent.tools._notes import attach_notes
from kebi.core.agent.tools._packing import pack_consult_result
from kebi.core.agent.tools._scope import anchor_to_corridor, itinerary_segments
from kebi.core.agent.tools._summaries import NEED_LOCATION, TITLES, found_summary
from kebi.core.agent.tools._with_timeout import tool_step_base_id, with_timeout
from kebi.core.agent.tools._working_location import maybe_working_location
from kebi.core.agent.tools.consult_models import ConsultCandidate, ConsultResult
from kebi.core.config import get_config
from kebi.core.knowledge.candidate_notes_service import CandidateNotesService
from kebi.core.knowledge.known_places_service import KnownPlacesService

logger = logging.getLogger(__name__)

_TOOL_NAME = "find_known"

_QUERY_DESC = (
    "What the answer needs to know, as a noun-phrase — 'monday night out', "
    "'ATM without fees', 'where to eat late'. Matched against the text and "
    "tags of the insider facts kebi holds for this area, so include the "
    "distinguishing words (the day, the timing, the constraint), not just the "
    "place type."
)

_TAGS_DESC = (
    "Claim-vocabulary tags sharpening the match (e.g. 'no_fee_atm', "
    "'open_late', 'lively', 'cash_only'). A tag hit counts for more than a "
    "word match. Omit when none clearly fits."
)

_LIMIT_DESC = "How many known places to return. Capped by config; defaults if omitted."


def build_find_known_tool(
    known_places: KnownPlacesService,
    notes_service: CandidateNotesService | None = None,
) -> BaseTool:
    """Factory — bind the request-scoped knowledge services into the tool."""

    @tool(_TOOL_NAME)
    async def find_known(
        query: Annotated[str, Field(description=_QUERY_DESC)],
        tool_call_id: Annotated[str, InjectedToolCallId],
        state: Annotated[AgentState, InjectedState],
        tags: Annotated[list[str] | None, Field(description=_TAGS_DESC)] = None,
        limit: Annotated[int | None, Field(description=_LIMIT_DESC)] = None,
    ) -> Command[Any]:
        """Places around here that kebi holds insider knowledge about."""
        cfg = get_config().agent
        effective_limit = max(
            1, min(limit or cfg.find_known.default_limit, cfg.find_known.max_limit)
        )
        return await with_timeout(
            tool_name=_TOOL_NAME,
            tool_call_id=tool_call_id,
            seconds=cfg.tool_timeouts_seconds.find_known,
            state=state,
            coro=_run_find_known(
                known_places=known_places,
                notes_service=notes_service,
                state=state,
                tool_call_id=tool_call_id,
                query=query,
                tags=tags,
                limit=effective_limit,
            ),
        )

    return find_known


def _summarise(result: ConsultResult) -> str:
    if result.empty_reason == "no_location":
        return NEED_LOCATION
    if result.empty_reason == "no_match":
        return "nothing I know of fits that here"
    return found_summary([c.place.place_name for c in result.candidates])


async def _run_find_known(
    *,
    known_places: KnownPlacesService,
    notes_service: CandidateNotesService | None,
    state: AgentState,
    tool_call_id: str,
    query: str,
    tags: list[str] | None,
    limit: int,
) -> Command[Any]:
    with set_tool(_TOOL_NAME):
        return await _run_find_known_impl(
            known_places=known_places,
            notes_service=notes_service,
            state=state,
            tool_call_id=tool_call_id,
            query=query,
            tags=tags,
            limit=limit,
        )


async def _run_find_known_impl(
    *,
    known_places: KnownPlacesService,
    notes_service: CandidateNotesService | None,
    state: AgentState,
    tool_call_id: str,
    query: str,
    tags: list[str] | None,
    limit: int,
) -> Command[Any]:
    user_id = state["user_id"]
    base_id = tool_step_base_id(_TOOL_NAME, state)
    started = emit_step_active(
        base_id, _TOOL_NAME, title=TITLES[_TOOL_NAME], source="agent"
    )

    working = maybe_working_location(state)
    if working is None:
        result = ConsultResult(candidates=[], empty_reason="no_location")
    else:
        working = anchor_to_corridor(working)
        # Shared retrieval context for every find below. The turn's real
        # weekday, so a schedule claim is retrievable without the user having
        # to say what day it is (ADR-138); daypart + season + taste values so
        # a claim is chosen for fitting this user, here, now, not for sharing
        # words with the question (ADR-142). Season stays calendar-only —
        # wet-vs-dry would be the stronger signal in the tropics but needs a
        # real weather source.
        find_context: dict[str, Any] = {
            "query": query,
            "tags": tags,
            "user_id": user_id,
            "day": local_weekday(state),
            "daypart": local_daypart(state),
            "season": local_season(state),
            "taste_values": state.get("taste_values") or [],
        }

        # A multi-stop trip runs one claims read per stop and per leg
        # (ADR-148), labelled and deduped in segment order — this is what
        # lets kebi know something about EVERY part of the trip, including a
        # claims-backed place in a city the user never named.
        segments = itinerary_segments(working)
        if segments:
            per_segment = get_config().agent.itinerary.per_segment_limit
            seen: set[str] = set()
            candidates: list[ConsultCandidate] = []
            for segment in segments:
                known = await known_places.find(
                    working=segment.working,
                    limit=min(per_segment, limit),
                    **find_context,
                )
                for k in known:
                    key = k.place.id or k.place.place_name
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(
                        ConsultCandidate(
                            place=k.place,
                            user_data=None,
                            source="known",
                            rrf_score=0.0,
                            notes=k.notes,
                            segment=segment.label,
                        )
                    )
            result = ConsultResult(
                candidates=candidates,
                empty_reason=None if candidates else "no_match",
            )
            # Area notes per STOP (legs have no area of their own): what
            # kebi knows about each city on the route, pooled in trip order
            # and deduped by claim id.
            area_notes = []
            seen_notes: set[str] = set()
            for segment in segments:
                if segment.on_the_way:
                    continue
                area_only = await attach_notes(
                    ConsultResult(),
                    notes_service=notes_service,
                    user_id=user_id,
                    working=segment.working,
                )
                for note in area_only.area_notes:
                    if note.id in seen_notes:
                        continue
                    seen_notes.add(note.id)
                    area_notes.append(note)
            result = result.model_copy(update={"area_notes": area_notes})
        else:
            known = await known_places.find(
                working=working,
                limit=limit,
                **find_context,
            )
            result = ConsultResult(
                candidates=[
                    ConsultCandidate(
                        place=k.place,
                        user_data=None,
                        source="known",
                        rrf_score=0.0,
                        # The notes ARE the reason this place is here, so
                        # they ride the candidate directly rather than
                        # waiting for `attach_notes` to re-read the same
                        # claims.
                        notes=k.notes,
                    )
                    for k in known
                ],
                empty_reason=None if known else "no_match",
            )
            # Re-run only for `area_notes` — the per-place notes are already
            # set above and `attach_notes` overwrites them with an
            # equivalent read.
            area_only = await attach_notes(
                ConsultResult(),
                notes_service=notes_service,
                user_id=user_id,
                working=working,
            )
            result = result.model_copy(update={"area_notes": area_only.area_notes})

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
