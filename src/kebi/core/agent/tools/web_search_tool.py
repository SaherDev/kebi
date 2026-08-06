"""`web_search` agent tool — the outside world (ADR-145).

The fifth tool, and the first that reads something kebi does not own. The
other four are the corpus: the user's saves, the place catalog, the claims
store. Everything they can answer is something kebi was already told, which
leaves a whole class of question with no path to an answer at all — when the
World Cup group stage is, which nights a festival runs, whether the ferry is
still cancelled, what the visa costs this month. A model answering those from
training weights is guessing with a confident voice, which is the failure
mode this repo has spent the last four ADRs removing from the *place* answers.

Unlike `research`, this tool takes no area arguments. The turn already
resolved a working location and the service localises the query with it —
one fewer argument for the model to fill in wrong, and no way for the search's
idea of "here" to drift from the conversation's.

Findings are ephemeral to the turn *and* durable: the tool returns them for
this answer, and the ones that read as lasting local facts are mined into the
claims store afterwards, off the critical path. That is the flywheel — the
first user to ask pays for the lookup, everyone after gets it from
`find_known` for free.
"""

from __future__ import annotations

import json
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
from kebi.core.agent.tools._agent_view import web_search_view
from kebi.core.agent.tools._summaries import TITLES
from kebi.core.agent.tools._with_timeout import tool_step_base_id, with_timeout
from kebi.core.agent.tools._working_location import maybe_working_location
from kebi.core.config import get_config
from kebi.core.web.models import WebSearchResult
from kebi.core.web.service import WebKnowledgeService

logger = logging.getLogger(__name__)

_TOOL_NAME = "web_search"

_QUERY_DESC = (
    "What to look up, phrased the way you would type it into a search box — "
    "'world cup 2026 group stage schedule', 'bali visa on arrival cost', "
    "'full moon party dates'. Do NOT add the city or country: the turn's "
    "location is added for you, and repeating it narrows the search to "
    "nothing. Keep it to the facts you need, not the user's whole sentence."
)

_FRESHNESS_DESC = (
    "How recent the results must be: 'day' | 'week' | 'month' | 'year'. Use "
    "it whenever the answer could have changed — schedules, prices, closures, "
    "what's on this weekend. Omit for things that don't move (history, "
    "customs, how a visa works), where a freshness filter only throws away "
    "the best sources."
)

_LIMIT_DESC = (
    "How many findings to pull. Capped by config; defaults if omitted. More "
    "is not better — they feed your prose, not a list."
)


def build_web_search_tool(web_knowledge: WebKnowledgeService) -> BaseTool:
    """Factory — bind the web-knowledge service into the tool."""

    @tool(_TOOL_NAME)
    async def web_search(
        query: Annotated[str, Field(description=_QUERY_DESC)],
        tool_call_id: Annotated[str, InjectedToolCallId],
        state: Annotated[AgentState, InjectedState],
        freshness: Annotated[str | None, Field(description=_FRESHNESS_DESC)] = None,
        limit: Annotated[int | None, Field(description=_LIMIT_DESC)] = None,
    ) -> Command[Any]:
        """Look something up on the web — dates, schedules, prices, what's on."""
        cfg = get_config().agent
        return await with_timeout(
            tool_name=_TOOL_NAME,
            tool_call_id=tool_call_id,
            seconds=cfg.tool_timeouts_seconds.web_search,
            state=state,
            coro=_run_web_search(
                web_knowledge=web_knowledge,
                state=state,
                tool_call_id=tool_call_id,
                query=query,
                freshness=freshness,
                limit=limit,
            ),
        )

    return web_search


def _summarise(result: WebSearchResult) -> str:
    if result.empty_reason == "no_provider":
        return "couldn't reach the web just now"
    if result.empty_reason:
        return "nothing current came back"
    count = len(result.findings)
    sources = [f.source for f in result.findings if f.source]
    preview = ", ".join(dict.fromkeys(sources))[:60]
    noun = "source" if count == 1 else "sources"
    return f"read {count} {noun}" + (f" — {preview}" if preview else "")


async def _run_web_search(
    *,
    web_knowledge: WebKnowledgeService,
    state: AgentState,
    tool_call_id: str,
    query: str,
    freshness: str | None,
    limit: int | None,
) -> Command[Any]:
    with set_tool(_TOOL_NAME):
        return await _run_web_search_impl(
            web_knowledge=web_knowledge,
            state=state,
            tool_call_id=tool_call_id,
            query=query,
            freshness=freshness,
            limit=limit,
        )


async def _run_web_search_impl(
    *,
    web_knowledge: WebKnowledgeService,
    state: AgentState,
    tool_call_id: str,
    query: str,
    freshness: str | None,
    limit: int | None,
) -> Command[Any]:
    base_id = tool_step_base_id(_TOOL_NAME, state)
    started = emit_step_active(
        base_id, _TOOL_NAME, title=TITLES[_TOOL_NAME], source="agent"
    )

    result = await web_knowledge.search(
        query=query,
        freshness=freshness,
        limit=limit,
        # No area args on this tool — the working location IS the scope, so
        # the search cannot drift from the conversation.
        working=maybe_working_location(state),
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

    tool_msg = ToolMessage(
        content=json.dumps(web_search_view(result)),
        tool_call_id=tool_call_id,
        name=_TOOL_NAME,
    )
    return Command(
        update={
            "messages": [tool_msg],
            # The full result (with URLs) rides the server-side channel: the
            # harvest needs the source_ref, the model does not need the link.
            "tool_payloads": (state.get("tool_payloads") or [])
            + [
                {
                    "tool": _TOOL_NAME,
                    "tool_call_id": tool_call_id,
                    "payload": result.model_dump(mode="json"),
                }
            ],
            "reasoning_steps": (state.get("reasoning_steps") or []) + [step],
            "tool_calls_used": state.get("tool_calls_used", 0) + 1,
        }
    )
