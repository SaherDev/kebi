"""recall_tool — @tool wrapper around RecallService (feature 028 M5/M9)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from pydantic import BaseModel, Field

from kebi.core.agent.state import AgentState
from kebi.core.agent.tools._emit import append_summary, build_emit_closure
from kebi.core.agent.tools._timeout import with_timeout
from kebi.core.places.models import PlaceObject
from kebi.core.recall.service import RecallService
from kebi.core.recall.types import RecallFilters


class RecallToolInput(BaseModel):
    """LLM-visible schema for the recall tool (SC-008)."""

    query: str | None = Field(
        default=None,
        description=(
            "Retrieval phrase, rewritten from the user's message into a short "
            "noun phrase describing the place type or topic. Examples: "
            "'find me a good ramen spot nearby' -> query='ramen restaurant'; "
            "'that museum in Bangkok' -> query='museum Bangkok'; 'the hotel I "
            "liked in Tokyo' -> query='hotel Tokyo'; 'saved places in Japan' "
            "-> query='Japan'. Pass null for meta-queries like 'show me all my "
            "saves' or 'places from TikTok' — that triggers filter-only mode "
            "and uses no embedding search."
        ),
    )
    filters: RecallFilters | None = Field(
        default=None,
        description=(
            "Structural filters on the user's saves. Mirror PlaceObject — "
            "place_type, subcategory, tags_include, nested attributes "
            "(cuisine, price_hint, ambiance, etc.)."
        ),
    )
    sort_by: Literal["relevance", "created_at"] = Field(
        default="relevance",
        description=(
            "Ordering. relevance = hybrid search score; created_at = most "
            "recently saved first (use for meta-queries)."
        ),
    )
    limit: int = Field(
        ge=1,
        le=10,
        description=(
            "How many saved places to return. YOU MUST choose based on the "
            "user's intent: 1 when they want one specific place, 3 for a "
            "short comparison, up to 10 for browsing/listing. Hard ceiling "
            "is 10 — never request more."
        ),
    )


def _filter_noun(filters: RecallFilters | None) -> str | None:
    if filters is None:
        return None
    if filters.subcategory:
        return filters.subcategory + "s"
    if filters.attributes is not None and filters.attributes.cuisine:
        return f"{filters.attributes.cuisine} places"
    if filters.place_type is not None:
        return filters.place_type.value.replace("_", " ")
    return None


def _recall_summary(
    query: str | None,
    filters: RecallFilters | None,
    places: list[PlaceObject],
) -> str:
    if query is None:
        what = _filter_noun(filters) or "places"
        if not places:
            return f"No saved {what} matched those filters"
        return f"Pulled your {len(places)} saved {what}"
    if not places:
        return f"Checked your saves for {query} — nothing matched"
    match_word = "match" if len(places) == 1 else "matches"
    return f"Checked your saves for {query} — found {len(places)} {match_word}"


def build_recall_tool(service: RecallService) -> BaseTool:
    """Return the @tool-decorated recall callable bound to `service`.

    No `args_schema=` here — see the save_tool docstring for why.
    """

    @tool("recall")
    async def recall_tool(
        query: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Retrieval phrase, rewritten from the user's message into "
                    "a short noun phrase describing the place type or topic. "
                    "Examples: 'find me a good ramen spot nearby' -> "
                    "query='ramen restaurant'; 'that museum in Bangkok' -> "
                    "query='museum Bangkok'; 'the hotel I liked in Tokyo' -> "
                    "query='hotel Tokyo'; 'saved places in Japan' -> "
                    "query='Japan'. Pass null for meta-queries like 'show me "
                    "all my saves' or 'places from TikTok' — that triggers "
                    "filter-only mode and uses no embedding search."
                ),
            ),
        ],
        filters: Annotated[
            RecallFilters | None,
            Field(
                default=None,
                description=(
                    "Structural filters on the user's saves. Mirror "
                    "PlaceObject — place_type, subcategory, tags_include, "
                    "nested attributes (cuisine, price_hint, ambiance, etc.)."
                ),
            ),
        ],
        sort_by: Annotated[
            Literal["relevance", "created_at"],
            Field(
                default="relevance",
                description=(
                    "Ordering. relevance = hybrid search score; created_at = "
                    "most recently saved first (use for meta-queries)."
                ),
            ),
        ],
        limit: Annotated[
            int,
            Field(
                ge=1,
                le=10,
                description=(
                    "How many saved places to return. YOU MUST choose based "
                    "on the user's intent: 1 when they want one specific "
                    "place, 3 for a short comparison, up to 10 for "
                    "browsing/listing. Hard ceiling is 10 — never request more."
                ),
            ),
        ],
        state: Annotated[AgentState, InjectedState],
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command[Any]:
        """Retrieve the user's saved places.

        Use this whenever the user wants to find, list, or recommend from
        their own saves. Also call this FIRST whenever the user asks for a
        recommendation — the result feeds into the consult tool
        automatically (you do not need to pass the places yourself; they
        are stored in agent state and picked up by consult on the next
        call).
        """

        async def _do_recall() -> Command[Any]:
            collected, emit = build_emit_closure("recall")
            raw_loc = state.get("location")
            loc_tuple: tuple[float, float] | None = (
                (raw_loc["lat"], raw_loc["lng"]) if raw_loc else None
            )
            response = await service.run(
                query=query,
                user_id=state["user_id"],
                filters=filters,
                sort_by=sort_by,
                location=loc_tuple,
                limit=limit,
                emit=emit,
            )
            places = [r.place for r in response.results]
            append_summary(collected, "recall", _recall_summary(query, filters, places))
            return Command(
                update={
                    "last_recall_results": places,
                    "reasoning_steps": (state.get("reasoning_steps") or []) + collected,
                    "messages": [
                        ToolMessage(
                            content=response.model_dump_json(),
                            tool_call_id=tool_call_id,
                        )
                    ],
                }
            )

        return await with_timeout("recall", tool_call_id, state, _do_recall())

    return recall_tool


__all__ = ["RecallToolInput", "build_recall_tool"]
