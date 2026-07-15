"""`research` agent tool — insider answers from the knowledge layer.

Fourth member of the consult family, and the knowledge layer's first
agent-facing reader. Where the three place tools surface *where to go*
(saved / suggested / discovered venues), `research` answers *what a local
knows* about a place or area: what to order, when to go, the low-fee ATM,
how to pay, what to avoid. It resolves the asked-about entity through the
staged verified-or-refuse resolver, reads that entity's approved claims by
exact key, ranks them in memory, and returns them as `ResearchNote`s for
the orchestrator to synthesize — grounded in what kebi actually knows,
never invented.

Unlike the place tools, the area args (`neighborhood`/`city`/`country`)
are *used*, not schema-parity decoration: they name the asked-about
entity. An unresolvable or ambiguous entity, an entity with no claims, or
claims that don't touch the asked topic all come back as an
`empty_reason` + clarification — the prompt turns those into a clarifying
question, never a fabricated tip and never a fall-through to discovery.

No LLM call, no paid provider: one indexed claims read plus at most one
free Nominatim geocode when the asked-about area isn't the working
location.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from pydantic import Field

from kebi.core.agent.reasoning import ReasoningStep
from kebi.core.agent.state import AgentState
from kebi.core.agent.stream_emit import emit_step_active, emit_step_done
from kebi.core.agent.tools._summaries import TITLES
from kebi.core.agent.tools._with_timeout import tool_step_base_id, with_timeout
from kebi.core.agent.tools._working_location import maybe_working_location
from kebi.core.config import get_config
from kebi.core.knowledge.research_models import ResearchResult
from kebi.core.knowledge.research_service import ResearchService

logger = logging.getLogger(__name__)

_TOOL_NAME = "research"

# LLM-facing arg docs. Deliberately NOT the shared `_search_args`
# descriptions: those describe place-search semantics (retrieval bias,
# hard constraints, ignored area overrides). Research's args mean
# different things — the area args name the asked-about entity and are
# resolved and used, and `tags` matches the claim vocabulary.

_QUERY_DESC = (
    "The research question's topic in noun-phrase form, e.g. 'low-fee ATMs', "
    "'what to order', 'coffee scene', 'is it safe at night'. Used to rank "
    "the entity's insider notes by relevance; keep it tight and topical."
)

_TAGS_DESC = (
    "Topic tags from the claim-tag vocabulary (core/knowledge/tags.py) that "
    "capture what's being asked, e.g. 'no_fee_atm', 'tipping', "
    "'safe_at_night', 'go_early', 'cash_only', 'walkable'. Sharpens the "
    "ranking; omit when no value clearly fits."
)

_NEIGHBORHOOD_DESC = (
    "The asked-about neighborhood (e.g. 'My Khe'), when the question names "
    "an area smaller than a city."
)

_CITY_DESC = (
    "The asked-about city (e.g. 'Da Nang'). Pass whenever the question "
    "names a city — even the current one."
)

_COUNTRY_DESC = (
    "The asked-about country (e.g. 'Vietnam'). IMPORTANT: also pass it "
    "alongside `city` whenever you know which country the asked-about city "
    "is in — it anchors resolution when the conversation was last about "
    "somewhere else entirely."
)

_LIMIT_DESC = (
    "How many insider notes to pull (they feed your prose answer, not a "
    "list). Capped by config; defaults if omitted."
)


def _build_command(
    *,
    state: AgentState,
    tool_call_id: str,
    result: ResearchResult,
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


def build_research_tool(research_service: ResearchService) -> BaseTool:
    """Factory — bind the research service into the tool."""

    @tool(_TOOL_NAME)
    async def research(
        query: Annotated[str, Field(description=_QUERY_DESC)],
        tool_call_id: Annotated[str, InjectedToolCallId],
        state: Annotated[AgentState, InjectedState],
        tags: Annotated[list[str] | None, Field(description=_TAGS_DESC)] = None,
        neighborhood: Annotated[
            str | None, Field(description=_NEIGHBORHOOD_DESC)
        ] = None,
        city: Annotated[str | None, Field(description=_CITY_DESC)] = None,
        country: Annotated[str | None, Field(description=_COUNTRY_DESC)] = None,
        limit: Annotated[int | None, Field(description=_LIMIT_DESC)] = None,
    ) -> Command[Any]:
        """Insider knowledge about a place or area, from what kebi knows."""
        timeout_s = get_config().agent.tool_timeouts_seconds.research
        return await with_timeout(
            tool_name=_TOOL_NAME,
            tool_call_id=tool_call_id,
            seconds=timeout_s,
            state=state,
            coro=_run_research(
                research_service=research_service,
                state=state,
                tool_call_id=tool_call_id,
                query=query,
                tags=tags,
                neighborhood=neighborhood,
                city=city,
                country=country,
                limit=limit,
            ),
        )

    return research


async def _run_research(
    *,
    research_service: ResearchService,
    state: AgentState,
    tool_call_id: str,
    query: str,
    tags: list[str] | None,
    neighborhood: str | None,
    city: str | None,
    country: str | None,
    limit: int | None,
) -> Command[Any]:
    """Inner body — resolve, read, rank. Wrapped by with_timeout."""
    steps: list[ReasoningStep] = []
    base_id = tool_step_base_id(_TOOL_NAME, state)
    # One user-visible row for the whole call (ADR-103), filled with the
    # outcome by `_finish`.
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

    result = await research_service.research(
        query=query,
        tags=tags,
        city=city,
        country=country,
        neighborhood=neighborhood,
        working_location=maybe_working_location(state),
        user_id=state["user_id"],
        limit=limit,
    )

    if result.empty_reason in ("unresolved", "ambiguous"):
        _finish("couldn't pin down the place", kind=result.empty_reason)
    elif result.empty_reason == "no_claims":
        _finish(
            f"nothing on {result.entity_name} yet", kind=result.empty_reason
        )
    elif result.empty_reason == "no_topic_match":
        _finish(
            f"nothing on that angle for {result.entity_name} yet",
            kind=result.empty_reason,
        )
    else:
        count = len(result.notes)
        noun = "note" if count == 1 else "notes"
        _finish(f"found {count} insider {noun} on {result.entity_name}")

    return _build_command(
        state=state,
        tool_call_id=tool_call_id,
        result=result,
        steps=steps,
    )
