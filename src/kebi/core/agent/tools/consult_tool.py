"""consult_tool — @tool wrapper around ConsultService (feature 028 M5/M9)."""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from pydantic import BaseModel, Field

from kebi.api.schemas.consult import ConsultResponse, Location
from kebi.core.agent.state import AgentState
from kebi.core.agent.tools._emit import append_summary, build_emit_closure
from kebi.core.agent.tools._timeout import with_timeout
from kebi.core.consult.service import ConsultService
from kebi.core.consult.types import NoMatchesError
from kebi.core.places.filters import ConsultFilters


class ConsultToolInput(BaseModel):
    """LLM-visible schema for the consult tool (SC-008/SC-009)."""

    query: str = Field(
        description=(
            "Retrieval phrase describing what to recommend, rewritten from "
            "the user's message. Examples: 'where should I eat tonight?' -> "
            "query='dinner restaurant'; 'I need a quiet place to work' -> "
            "query='quiet cafe laptop work'; 'something to do on a rainy "
            "afternoon' -> query='indoor activity'; 'a hotel near Shibuya' "
            "-> query='hotel Shibuya'."
        ),
    )
    place_suggestions: list[str] = Field(
        default_factory=list,
        description=(
            "Specific real place names from your world knowledge that fit "
            "the user's request and location. Validated against the places "
            "provider — unrecognised names are dropped. Always populate when "
            "you know the area. Limit 5. Leave empty only if you have no "
            "knowledge of the area."
        ),
    )
    filters: ConsultFilters = Field(
        description=(
            "Structural + discovery filters: radius_m, search_location_name, "
            "place_type, price range."
        ),
    )
    limit: int = Field(
        ge=1,
        le=5,
        description=(
            "How many recommendations to return. YOU MUST choose based on "
            "the user's intent and context: 1 when they want a single "
            "confident pick ('where should I go tonight'), 2–3 when "
            "comparing options, up to 5 when they explicitly ask for a few "
            "choices. Prefer fewer. Hard ceiling is 5 — never request more."
        ),
    )
    preference_context: str | None = Field(
        default=None,
        description=(
            "One- or two-sentence summary composed from "
            "taste_profile_summary and memory_summary, limited to signals "
            "RELEVANT to this request. Example for a dinner request: "
            "'Prefers casual spots over formal. Wheelchair user. Avoids "
            "pork.' Example for a museum request: 'Likes contemporary art. "
            "Visits on weekdays. Wheelchair user.' Omit irrelevant signals."
        ),
    )


def _consult_summary(response: ConsultResponse) -> str:
    saved = sum(1 for r in response.results if r.source == "saved")
    external = sum(
        1 for r in response.results if r.source in ("discovered", "suggested")
    )
    total = saved + external
    if total == 0:
        return "Nothing matched nearby"
    if saved == 0:
        return f"Ranked {external} nearby options"
    if external == 0:
        return f"Ranked {saved} from your saves"
    return f"Ranked {total} options ({saved} saved + {external} nearby)"


def build_consult_tool(service: ConsultService) -> BaseTool:
    """Return the @tool-decorated consult callable bound to `service`.

    No `args_schema=` is passed to `@tool` — that short-circuits
    LangGraph's `Annotated[..., InjectedState / InjectedToolCallId]`
    inspection, so injection never wires up. Let `@tool` build the
    schema from the function signature instead.
    """

    @tool("consult")
    async def consult_tool(
        query: Annotated[
            str,
            Field(
                description=(
                    "Retrieval phrase describing what to recommend, rewritten "
                    "from the user's message. Examples: 'where should I eat "
                    "tonight?' -> query='dinner restaurant'; 'I need a quiet "
                    "place to work' -> query='quiet cafe laptop work'; "
                    "'something to do on a rainy afternoon' -> query='indoor "
                    "activity'; 'a hotel near Shibuya' -> query='hotel "
                    "Shibuya'."
                ),
            ),
        ],
        filters: Annotated[
            ConsultFilters,
            Field(
                description=(
                    "Structural + discovery filters: radius_m, "
                    "search_location_name, place_type, price range."
                ),
            ),
        ],
        limit: Annotated[
            int,
            Field(
                ge=1,
                le=5,
                description=(
                    "How many recommendations to return. YOU MUST choose "
                    "based on the user's intent and context: 1 when they "
                    "want a single confident pick ('where should I go "
                    "tonight'), 2–3 when comparing options, up to 5 when "
                    "they explicitly ask for a few choices. Prefer fewer. "
                    "Hard ceiling is 5 — never request more."
                ),
            ),
        ],
        state: Annotated[AgentState, InjectedState],
        tool_call_id: Annotated[str, InjectedToolCallId],
        place_suggestions: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Specific real place names from your world knowledge that "
                    "fit the user's request and location. These are validated "
                    "against the places provider — names that don't match a "
                    "real venue are dropped automatically. Always populate this "
                    "when you know the area. Limit 5. "
                    "Examples: ['Rijksmuseum', 'Anne Frank House', 'Vondelpark'] "
                    "for Amsterdam sightseeing; ['Leidseplein cafes', "
                    "'Brouwerij 't IJ'] for Amsterdam drinks; "
                    "['Ichiran', 'Fuji Ramen'] for Tokyo ramen. "
                    "Leave empty ONLY if you have no knowledge of the area."
                ),
            ),
        ] = None,
        preference_context: Annotated[
            str | None,
            Field(
                description=(
                    "One- or two-sentence summary composed from "
                    "taste_profile_summary and memory_summary, limited to "
                    "signals RELEVANT to this request. Example for a dinner "
                    "request: 'Prefers casual spots over formal. Wheelchair "
                    "user. Avoids pork.' Example for a museum request: "
                    "'Likes contemporary art. Visits on weekdays. Wheelchair "
                    "user.' Omit irrelevant signals."
                ),
            ),
        ] = None,
    ) -> Command[Any]:
        """Recommend a place.

        Merges the user's saved places (from the previous recall call,
        available automatically via agent state) with externally
        discovered candidates, deduplicates, and returns ranked results.

        Call recall FIRST in the same turn. If the user has no saved
        matches, call recall anyway — consult will work with the empty
        list and return discoveries only.
        """

        async def _do_consult() -> Command[Any]:
            collected, emit = build_emit_closure("consult")
            saved_places = state.get("last_recall_results") or []
            raw_loc = state.get("location")
            loc: Location | None = (
                Location(lat=raw_loc["lat"], lng=raw_loc["lng"]) if raw_loc else None
            )
            if place_suggestions:
                filters.place_suggestions = place_suggestions
            try:
                response = await service.consult(
                    user_id=state["user_id"],
                    query=query,
                    saved_places=saved_places,
                    filters=filters,
                    limit=limit,
                    location=loc,
                    preference_context=preference_context,
                    signal_tier="active",
                    emit=emit,
                )
            except NoMatchesError:
                response = ConsultResponse(recommendation_id=None, results=[])
            append_summary(collected, "consult", _consult_summary(response))
            return Command(
                update={
                    "reasoning_steps": (state.get("reasoning_steps") or []) + collected,
                    "messages": [
                        ToolMessage(
                            content=response.model_dump_json(),
                            tool_call_id=tool_call_id,
                        )
                    ],
                }
            )

        return await with_timeout("consult", tool_call_id, state, _do_consult())

    return consult_tool


__all__ = ["ConsultToolInput", "build_consult_tool"]
