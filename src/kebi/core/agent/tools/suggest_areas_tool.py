"""`suggest_areas` agent tool — the areas the agent named, verified and placed.

Fifth member of the consult family, and the one that lets an area *be* an
answer (location-kinds Step 6).

**The agent does the naming.** There is no model inside this tool — no
candidate namer, no fallback. The orchestrator holds the query, the taste
summary and the conversation, and it knows which neighborhoods are worth
staying in; delegating that to a smaller model is precisely the mistake
ADR-140 was written against. What the tool adds is everything the agent
cannot know on its own: whether the area is real, where it is, how big it is,
and what kebi has accumulated about it.

So it does two jobs at once, and the second is the important one:

  1. **Cards.** A verified area comes back as an `area` candidate — a shaded
     extent with a one-line why, never a pin. This is what ADR-137's line
     requires: prose may carry the agent's knowledge, but anything tappable
     has to come from a tool result.
  2. **Anchors.** The verified entities land on `state["area_anchors"]`, so
     the place tools that run next search *at each named area* rather than
     around the turn's origin (ADR-140). This is what stops an answer that
     names Hue, Hoi An and the pass in prose and pins nothing at any of them.

Cost is deliberately near-zero so it can run *before* the place tools rather
than instead of them: no provider place call, no LLM, one batched claims read,
and a geocode only when an area is new to the store.

Refusals are narrated, never silent. A name that does not verify is reported
back so the agent can say so — and is never substituted with somewhere nearby,
which is the rule that keeps an unverifiable route name from being stored as
the city it happens to sit in.
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
from kebi.core.agent.reasoning import ReasoningStep
from kebi.core.agent.state import AgentState
from kebi.core.agent.stream_emit import emit_step_active, emit_step_done
from kebi.core.agent.tools._summaries import TITLES, areas_summary
from kebi.core.agent.tools._with_timeout import tool_step_base_id, with_timeout
from kebi.core.agent.tools._working_location import maybe_working_location
from kebi.core.agent.tools.consult_models import (
    ConsultCandidate,
    ConsultResult,
    EmptyReason,
)
from kebi.core.areas.suggestion_service import AreaSuggestionService
from kebi.core.config import get_config

logger = logging.getLogger(__name__)

_TOOL_NAME = "suggest_areas"

_NAMES_DESC = (
    "The areas YOU think are worth putting forward, most relevant first — "
    "neighborhoods ('An Thuong'), cities ('Hoi An'), regions ('Quang Nam'), "
    "or named geography ('Hai Van Pass', 'Lang Co Beach'). Use your own "
    "knowledge of the place; this tool verifies and places them, it does not "
    "pick them for you. Do NOT pass a multi-stop journey name ('Ha Giang "
    "Loop') — it has no fixed footprint and will be refused."
)

_CITY_DESC = (
    "The city these areas sit in, when they are sub-city ('Da Nang' for 'An "
    "Thuong'). Disambiguates a bare neighborhood name; omit for cities and "
    "regions."
)

_COUNTRY_DESC = (
    "The country the areas are in. Pass it whenever you know it — resolution "
    "is country-scoped, and without it the tool falls back to the turn's "
    "working location and refuses everything if there isn't one."
)

_LIMIT_DESC = (
    "How many areas to return as cards. Capped by config; defaults if omitted."
)


def _build_command(
    *,
    state: AgentState,
    tool_call_id: str,
    result: ConsultResult,
    steps: list[ReasoningStep],
    anchors: list[dict[str, Any]],
) -> Command[Any]:
    """Pack the result, the reasoning steps, and the anchors into one Command.

    `area_anchors` rides the same update as the tool message so the place
    tools that run next in this turn see the verified entities. Plain
    overwrite: the areas this question is about replace the last one's.
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
            "area_anchors": anchors,
        }
    )


def build_suggest_areas_tool(service: AreaSuggestionService) -> BaseTool:
    """Factory — bind the request-scoped suggestion service into the tool."""

    @tool(_TOOL_NAME)
    async def suggest_areas(
        names: Annotated[list[str], Field(description=_NAMES_DESC)],
        tool_call_id: Annotated[str, InjectedToolCallId],
        state: Annotated[AgentState, InjectedState],
        city: Annotated[str | None, Field(description=_CITY_DESC)] = None,
        country: Annotated[str | None, Field(description=_COUNTRY_DESC)] = None,
        limit: Annotated[int | None, Field(description=_LIMIT_DESC)] = None,
    ) -> Command[Any]:
        """Verify areas you're putting forward, and anchor the search on them."""
        cfg = get_config().agent
        effective_limit = max(
            1,
            min(limit or cfg.suggest_areas.default_limit, cfg.suggest_areas.max_limit),
        )
        return await with_timeout(
            tool_name=_TOOL_NAME,
            tool_call_id=tool_call_id,
            seconds=cfg.tool_timeouts_seconds.suggest_areas,
            state=state,
            coro=_run_suggest_areas(
                service=service,
                state=state,
                tool_call_id=tool_call_id,
                names=names,
                city=city,
                country=country,
                limit=effective_limit,
            ),
        )

    return suggest_areas


async def _run_suggest_areas(
    *,
    service: AreaSuggestionService,
    state: AgentState,
    tool_call_id: str,
    names: list[str],
    city: str | None,
    country: str | None,
    limit: int,
) -> Command[Any]:
    """Inner body — resolve, attach claims, publish anchors."""
    with set_tool(_TOOL_NAME):
        return await _run_suggest_areas_impl(
            service=service,
            state=state,
            tool_call_id=tool_call_id,
            names=names,
            city=city,
            country=country,
            limit=limit,
        )


async def _run_suggest_areas_impl(
    *,
    service: AreaSuggestionService,
    state: AgentState,
    tool_call_id: str,
    names: list[str],
    city: str | None,
    country: str | None,
    limit: int,
) -> Command[Any]:
    steps: list[ReasoningStep] = []
    base_id = tool_step_base_id(_TOOL_NAME, state)
    # One user-visible row for the whole call (ADR-103), filled by `_finish`.
    outcome_started = emit_step_active(
        base_id, _TOOL_NAME, title=TITLES[_TOOL_NAME], source="agent"
    )

    def _finish(summary: str, *, kind: str = "summary") -> None:
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

    def _empty(reason: EmptyReason, summary: str, kind: str) -> Command[Any]:
        _finish(summary, kind=kind)
        return _build_command(
            state=state,
            tool_call_id=tool_call_id,
            result=ConsultResult(candidates=[], empty_reason=reason),
            steps=steps,
            anchors=[],
        )

    if not [n for n in names if n.strip()]:
        return _empty("no_match", "no areas to check", "no_names")

    try:
        result = await service.suggest(
            names=names,
            user_id=state["user_id"],
            country=country,
            city=city,
            working_location=maybe_working_location(state),
        )
    except Exception as exc:
        logger.warning("suggest_areas resolution failed: %s", exc, exc_info=True)
        return _empty("error", "couldn't check those areas", "provider_error")

    if not result.suggestions:
        # Every name refused. An honest empty, not an error: the agent knows
        # which names did not check out and answers without them.
        return _empty("no_match", "none of those checked out", "all_refused")

    kept = result.suggestions[:limit]
    _finish(areas_summary([s.summary.name for s in kept], refused=len(result.refused)))

    candidates = [ConsultCandidate.for_area(suggestion.summary) for suggestion in kept]
    return _build_command(
        state=state,
        tool_call_id=tool_call_id,
        result=ConsultResult(candidates=candidates, empty_reason=None),
        steps=steps,
        anchors=[suggestion.entity.model_dump(mode="json") for suggestion in kept],
    )
