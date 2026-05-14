"""save_tool — @tool wrapper around ExtractionService (feature 028 M5/M9).

Spec 030 Phase 6 cleanup: the legacy `needs_review` NodeInterrupt
flow is gone. Under ADR-071 the extraction flow saves every picker
output unconditionally, so there's nothing to interrupt on. Each
item in `response.results` is just a saved place; the per-item
`status` field has been removed from the schema entirely.
"""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from pydantic import BaseModel, Field

from kebi.api.schemas.extract_place import ExtractPlaceResponse
from kebi.core.agent.state import AgentState
from kebi.core.agent.tools._emit import append_summary, build_emit_closure
from kebi.core.agent.tools._timeout import with_timeout
from kebi.core.extraction.service import ExtractionService


class SaveToolInput(BaseModel):
    """LLM-visible schema for the save tool (SC-008)."""

    raw_input: str = Field(
        description=(
            "Call when the user shares a URL (TikTok, Instagram, YouTube) "
            "or names a specific place they want to save. Pass the raw URL "
            "or text — do not reformat."
        ),
    )


def _save_summary(response: ExtractPlaceResponse) -> str:
    if response.status == "failed":
        return "Couldn't extract a place from that"
    if response.status == "pending":
        return "Extraction in progress — I'll update you shortly"
    # status == "completed" with at least one result.
    names = [r.place.place_name for r in response.results]
    if len(names) == 1:
        return f"Saved {names[0]} to your places"
    return f"Saved {len(names)} places: {', '.join(names)}"


def build_save_tool(service: ExtractionService) -> BaseTool:
    """Return the @tool-decorated save callable bound to `service`.

    No `args_schema=` here: LangGraph's `ToolNode` only honors
    `Annotated[..., InjectedState]` / `InjectedToolCallId` on the
    function signature when `@tool` is allowed to build the args schema
    from that signature directly. Passing an explicit `args_schema`
    short-circuits that inspection and the injected args are then called
    as missing positional arguments.
    """

    @tool("save")
    async def save_tool(
        raw_input: Annotated[
            str,
            Field(
                description=(
                    "Raw URL (TikTok, Instagram, YouTube) or the text naming "
                    "a specific place to save. Do not reformat — pass "
                    "verbatim."
                )
            ),
        ],
        state: Annotated[AgentState, InjectedState],
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command[Any]:
        """Save a place the user shared (URL or free text).

        Call when the user shares a URL (TikTok, Instagram, YouTube) or
        names a specific place they want to save. Pass the raw URL or
        text — do not reformat.
        """

        async def _do_save() -> Command[Any]:
            collected, emit = build_emit_closure("save")
            response = await service.run(raw_input, state["user_id"], emit=emit)
            append_summary(collected, "save", _save_summary(response))
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

        return await with_timeout("save", tool_call_id, state, _do_save())

    return save_tool


__all__ = ["SaveToolInput", "build_save_tool"]
