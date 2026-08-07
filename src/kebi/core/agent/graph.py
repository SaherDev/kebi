"""LangGraph agent skeleton: nodes, routing, factory (feature 027 M3, ADR-062).

Structural only — M3 does not wire the graph to `/v1/chat` (that is M6).
`agent_node` accepts an injected LLM; M3 tests drive it with a fake LLM
(per clarification). Real orchestrator wiring lands in M6's lazy graph
construction.

M9 additions: error wrapping in agent_node (increments error_count on
LLM failure) and debug diagnostic steps in fallback_node.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from kebi.core.agent._trace_context import feature_span
from kebi.core.agent.location import (
    CorridorTarget,
    ItineraryAnchor,
    LocationResolution,
    WorkingLocation,
    density_class,
    resolve_radius,
)
from kebi.core.agent.messages import extract_text_content
from kebi.core.agent.reasoning import ReasoningStep
from kebi.core.agent.state import AgentState
from kebi.core.agent.stream_emit import emit_step_active, emit_step_done
from kebi.core.config import get_config
from kebi.core.places.nominatim_geocoding_client import GeocodingError
from kebi.core.utils.geo import haversine_m
from kebi.providers.tracing import TracingSpan, get_tracing_client

logger = logging.getLogger(__name__)

# Number of attempts for each LLM call in agent_node. Anthropic's API
# occasionally returns TLS handshake errors (SSLV3_ALERT_BAD_RECORD_MAC)
# or dropped connections — a small bounded retry absorbs these without
# surfacing to the user. Exponential backoff starting at 500ms.
_LLM_MAX_ATTEMPTS = 3
_LLM_BACKOFF_BASE_SECONDS = 0.5


async def _invoke_llm_with_retry(
    bound: Any,
    conversation: list[Any],
    *,
    make_span: Callable[[], TracingSpan] | None = None,
    extract_usage: Callable[[Any], dict[str, int]] | None = None,
) -> Any:
    """Call `bound.ainvoke(conversation)` with bounded retry.

    Retries any Exception up to `_LLM_MAX_ATTEMPTS` total attempts with
    exponential backoff. Re-raises the last exception on final failure.

    When `make_span` is supplied, opens a fresh Langfuse generation per
    attempt so a turn that succeeds after N retries surfaces as N
    observations (N-1 ERROR + 1 OK) rather than one cheap-looking span.
    `extract_usage` pulls token counts off the successful result so the
    span carries usage; pass None when the underlying call has no usage
    metadata.
    """
    last_exc: Exception | None = None
    for attempt in range(_LLM_MAX_ATTEMPTS):
        span = make_span() if make_span is not None else None
        try:
            result = await bound.ainvoke(conversation)
        except Exception as exc:
            last_exc = exc
            if span is not None:
                span.end(
                    level="ERROR",
                    output={"error": str(exc), "attempt": attempt + 1},
                )
            logger.warning(
                "LLM attempt %d/%d failed: %s",
                attempt + 1,
                _LLM_MAX_ATTEMPTS,
                exc,
            )
            if attempt < _LLM_MAX_ATTEMPTS - 1:
                await asyncio.sleep(_LLM_BACKOFF_BASE_SECONDS * (2**attempt))
            continue
        if span is not None:
            usage = extract_usage(result) if extract_usage is not None else {}
            span.end(usage=usage, output={"attempt": attempt + 1})
        return result
    assert last_exc is not None
    raise last_exc


def _ai_message_usage(msg: Any) -> dict[str, int]:
    """Pull Langfuse-shaped usage off a LangChain `AIMessage`.

    LangChain Anthropic / OpenAI populate `usage_metadata` with
    `{"input_tokens", "output_tokens", "total_tokens"}` (and optionally
    a `cache_*` breakdown we don't propagate yet). Returns `{}` when
    the metadata is missing so callers can pass the result straight to
    `span.end(usage=...)`.
    """
    meta = getattr(msg, "usage_metadata", None)
    if not isinstance(meta, dict):
        return {}
    input_t = int(meta.get("input_tokens", 0) or 0)
    output_t = int(meta.get("output_tokens", 0) or 0)
    total_t = int(meta.get("total_tokens", 0) or 0) or (input_t + output_t)
    return {"input": input_t, "output": output_t, "total": total_t}


def _structured_usage(result: Any) -> dict[str, int]:
    """Pull usage off a structured-output result `{"raw", "parsed", ...}`.

    LangChain's `with_structured_output(..., include_raw=True)` returns
    a dict whose `"raw"` slot is the original `AIMessage`. Falls back
    to `{}` when the shape diverges (cached `with_structured_output`
    without `include_raw`, etc.).
    """
    if isinstance(result, dict):
        raw = result.get("raw")
        if raw is not None:
            return _ai_message_usage(raw)
    return _ai_message_usage(result)


# Shown for the user-visible `agent.tool_decision` step when the LLM
# returns empty content (a silent tool call, no preamble prose). Plain,
# non-technical wording — this is a user-visible reasoning step.
_DIRECT_RESPONSE_FALLBACK = "Let me look into that…"

# Node names are re-used by tests asserting graph structure.
NODE_RESOLVE_LOCATION = "resolve_location"
NODE_AGENT = "agent"
NODE_TOOLS = "tools"
NODE_FALLBACK = "fallback"
NODE_FINALIZE = "finalize"
NODE_SCRUB_TOOL_RESULTS = "scrub_tool_results"
NODE_TRIP_GUARD = "trip_guard"

# Fallback message shown to the user when the graph terminates early.
_FALLBACK_MESSAGE = (
    "Something went wrong on my side — try again with a bit more detail?"
)
# Distinct terminal message for the tool-call-cap branch — the agent burned
# its tool budget without putting together a useful list. The cause is
# almost always an under-specified intent, not a system fault, so the
# wording asks for more detail rather than apologising for a failure.
_TOOL_CAP_MESSAGE = (
    "I tried a few angles and couldn't put together a useful list — "
    "give me a bit more detail and I'll try again."
)


def _strip_orphaned_tool_results(messages: list[Any]) -> tuple[list[Any], int]:
    """Remove ToolMessages whose tool_call_id has no matching AIMessage in the window.

    History trimming can cut the AIMessage that triggered a tool call while keeping
    the ToolMessage response, producing a tool_result block with no tool_use.
    Anthropic rejects this with a 400. Strip those ToolMessages before sending.

    Returns the cleaned message list and the count of stripped messages.
    """
    known_ids: set[str] = set()
    for msg in messages:
        if isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", None) or []:
                if tc.get("id"):
                    known_ids.add(tc["id"])

    stripped = 0
    result = []
    for msg in messages:
        if isinstance(msg, ToolMessage):
            tcid = getattr(msg, "tool_call_id", None)
            if tcid and tcid not in known_ids:
                stripped += 1
                continue
        result.append(msg)
    return result, stripped


def _sanitize_orphaned_tool_calls(messages: list[Any]) -> tuple[list[Any], int]:
    """Inject placeholder ToolMessages for orphaned tool_use blocks.

    When a tool call is interrupted (timeout, server restart), the checkpointer
    stores an AIMessage with tool_calls but no subsequent ToolMessages. Sending
    that history to Anthropic causes a 400. We detect the condition and inject
    synthetic error ToolMessages so the conversation remains valid.

    Returns the sanitized message list and the count of injected placeholders.
    """
    result: list[Any] = []
    injected = 0
    for i, msg in enumerate(messages):
        result.append(msg)
        if not isinstance(msg, AIMessage):
            continue
        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            continue
        expected_ids = {tc["id"] for tc in tool_calls if tc.get("id")}
        satisfied_ids: set[str] = set()
        j = i + 1
        while j < len(messages) and isinstance(messages[j], ToolMessage):
            tcid = getattr(messages[j], "tool_call_id", None)
            if tcid:
                satisfied_ids.add(tcid)
            j += 1
        for tc in tool_calls:
            if tc.get("id") in expected_ids - satisfied_ids:
                result.append(
                    ToolMessage(
                        content="Tool call did not complete — please continue.",
                        tool_call_id=tc["id"],
                    )
                )
                injected += 1
    return result, injected


# Cap on names included in a compacted breadcrumb. Six is enough for the LLM
# to disambiguate "the third one" or "show me Bun Bo Hue again" while keeping
# the breadcrumb in the ~100-token range.
_BREADCRUMB_NAME_CAP = 6


def _extract_place_names(results: list[Any]) -> list[str]:
    """Pull place_name from `results[].place.place_name` (recall/consult/save shape)."""
    names: list[str] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        place = r.get("place")
        nm: str | None = None
        if isinstance(place, dict):
            value = place.get("place_name")
            if isinstance(value, str):
                nm = value
        if nm is None:
            # Lean agent view (ADR-139) names the field `name`.
            for key in ("place_name", "name"):
                value = r.get(key)
                if isinstance(value, str):
                    nm = value
                    break
        if nm:
            names.append(nm)
    return names


def _format_names(names: list[str]) -> str:
    if len(names) <= _BREADCRUMB_NAME_CAP:
        return ", ".join(names)
    head = ", ".join(names[:_BREADCRUMB_NAME_CAP])
    return f"{head}, … (+{len(names) - _BREADCRUMB_NAME_CAP} more)"


def _summarize_tool_payload(msg: ToolMessage) -> str:
    """Squeeze a ToolMessage's JSON body into a one-line breadcrumb.

    Full payloads (recall/consult/save responses) run 2-5KB each. Once the
    LLM has reacted to a tool result, the JSON is dead weight on the next
    turn — but bare counts ("returned 3 results") strip away cross-turn
    referenceability ("show me Bun Bo Hue again", "the third one"). So the
    breadcrumb keeps the place names (capped at `_BREADCRUMB_NAME_CAP`)
    while dropping the rest. Costs ~100 tokens vs. ~500-2500 for the full
    JSON.
    """
    raw = extract_text_content(getattr(msg, "content", None))
    name = getattr(msg, "name", None) or "tool"
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return f"[{name}] earlier result elided ({len(raw)} chars)"
    if isinstance(data, dict):
        results = data.get("results")
        if results is None and isinstance(data.get("candidates"), list):
            results = data["candidates"]
        if isinstance(results, list):
            names = _extract_place_names(results)
            count = len(results)
            if names:
                return (
                    f"[{name}] earlier call returned {count} result(s): "
                    f"{_format_names(names)}"
                )
            return f"[{name}] earlier call returned {count} result(s); details elided"
        status = data.get("status")
        if status:
            return f"[{name}] earlier call status={status}; details elided"
        if "error" in data:
            return f"[{name}] earlier call errored ({data.get('type', 'error')})"
    return f"[{name}] earlier result elided"


def _compact_old_tool_results(
    messages: list[Any], keep_recent: int
) -> tuple[list[Any], int]:
    """Replace ToolMessage bodies older than the last `keep_recent` with breadcrumbs.

    Produces fresh ToolMessage objects with the same `tool_call_id` so the
    Anthropic tool_use ↔ tool_result pairing stays valid. Does not mutate
    state — the original messages in the checkpointer stay intact; this only
    rewrites the per-call conversation list.

    `keep_recent=0` compacts every ToolMessage. Negative values are a no-op.
    """
    if keep_recent < 0:
        return messages, 0
    tool_indices = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
    if len(tool_indices) <= keep_recent:
        return messages, 0
    cutoff_slice = tool_indices[: -keep_recent or None]
    cutoff = set(cutoff_slice)
    result: list[Any] = []
    compacted = 0
    for i, msg in enumerate(messages):
        if i in cutoff:
            result.append(
                ToolMessage(
                    content=_summarize_tool_payload(msg),
                    tool_call_id=getattr(msg, "tool_call_id", "") or "",
                    name=getattr(msg, "name", None),
                )
            )
            compacted += 1
        else:
            result.append(msg)
    return result, compacted


def _render_location_context(state: AgentState) -> str:
    """Render the `{location_context}` slot from the resolved working location.

    The `resolve_location` node has already run (or been gated out) by the
    time this is called: it leaves either a resolved `working_location`, a
    `location_clarification` reason, or neither.

    Defensive: when the gate skips `resolve_location` and no prior working
    location is carried, the `working_location` slot can still hold the
    `LOCATION_INHERIT` sentinel string (a truthy non-dict). Treat
    anything that is not a real dict as "no location resolved" so the
    renderer never crashes on `.get()` against a string.
    """
    working = state.get("working_location")
    if isinstance(working, dict) and working:
        parts = [
            working.get("neighborhood"),
            working.get("city"),
            working.get("country"),
        ]
        place = ", ".join(p for p in parts if p)
        itinerary = working.get("itinerary") or []
        if working.get("scope_shape") == "itinerary" and len(itinerary) >= 2:
            stops = ", then ".join(a.get("name", "") for a in itinerary)
            return (
                f"This turn is a multi-stop trip: {stops}. The working "
                f"location anchors at the first stop ({place}), but the "
                "place tools already search every stop AND the legs between "
                "them — results come back labelled with the part of the trip "
                "they belong to. Build the answer stop by stop, in trip "
                "order."
            )
        return (
            f"Working location for this turn: {place} "
            f"(lat={working.get('lat')}, lng={working.get('lng')}). "
            "Operate against this location for anything place-related. The "
            "working location is resolved fresh each turn — if the user names "
            "a new place it becomes the new working location; a follow-up "
            'like "and what else" stays with the current one.'
        )
    clarification = state.get("location_clarification")
    if clarification:
        return (
            "The location for this turn could not be determined: "
            f"{clarification} Ask the user to clarify which place they mean "
            "before giving any location-specific advice. Do not guess."
        )
    return (
        "No location resolved for this turn. If the user asks for anything "
        "location-specific, ask which city or area they mean."
    )


_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def parse_local_time(raw: str | None) -> datetime | None:
    """Parse the client-supplied ISO-8601 local time, or None.

    Never raises: a malformed clock from a client must degrade to "no time
    known" (the agent then avoids schedule claims) rather than fail the turn.
    """
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        logger.warning("unparseable local_time from client: %r", raw)
        return None


def local_weekday(state: AgentState) -> str | None:
    """The turn's day of week ("Monday"), or None when the clock is unknown."""
    parsed = parse_local_time(state.get("local_time"))
    return None if parsed is None else _WEEKDAYS[parsed.weekday()]


# Hour bands from the places vocabulary (`TimeTag`), so a claim tagged
# `evening` and a turn at 19:30 speak the same language. Ordered, and read as
# half-open ranges; `all_day` is deliberately absent — it is a neutral value a
# claim carries, never a time a turn is at.
_DAYPARTS: tuple[tuple[int, int, str], ...] = (
    (6, 11, "morning"),
    (11, 12, "brunch"),
    (12, 15, "lunch"),
    (15, 18, "afternoon"),
    (18, 21, "evening"),
    (21, 24, "night"),
    (0, 6, "late_night"),
)


def local_daypart(state: AgentState) -> str | None:
    """Which part of the day it is for the user, in vocabulary terms.

    This is what lets a "best at sunset" claim outrank a breakfast one at 17:00
    without the orchestrator having to encode the hour into its query text.
    None when the client sent no clock — the same absent-means-absent rule as
    the weekday.
    """
    parsed = parse_local_time(state.get("local_time"))
    if parsed is None:
        return None
    hour = parsed.hour
    for start, end, name in _DAYPARTS:
        if start <= hour < end:
            return name
    return None


def local_season(state: AgentState) -> str | None:
    """The calendar season where the user is, or None.

    Northern/southern hemisphere is decided by the working location's latitude,
    because a July claim about "summer terraces" is wrong advice in Bali. In
    the tropics there is no meaningful calendar season — the useful axis is wet
    versus dry, which varies by region and is not derivable from a date — so
    this returns None there rather than asserting something false.
    """
    parsed = parse_local_time(state.get("local_time"))
    working = state.get("working_location")
    if parsed is None or not isinstance(working, dict):
        return None
    lat = working.get("lat")
    if not isinstance(lat, int | float):
        return None
    if abs(lat) < 23.5:
        return None
    northern = [
        "winter",
        "winter",
        "spring",
        "spring",
        "spring",
        "summer",
        "summer",
        "summer",
        "autumn",
        "autumn",
        "autumn",
        "winter",
    ]
    season = northern[parsed.month - 1]
    if lat >= 0:
        return season
    return {
        "winter": "summer",
        "spring": "autumn",
        "summer": "winter",
        "autumn": "spring",
    }[season]


def _render_time_context(state: AgentState) -> str:
    """Render the `{time_context}` slot — what day and hour it is for the user.

    Day of week is load-bearing for a scheduled answer (ADR-138): a claim
    saying Monday is a venue's big night is only usable if the agent knows
    today is Monday. The clock comes from the client, so it can be absent —
    and when it is, the slot says so plainly, because an agent that assumes a
    day will confidently answer for the wrong one.
    """
    parsed = parse_local_time(state.get("local_time"))
    if parsed is None:
        return (
            "The user's local date and time are unknown this turn. Do not "
            "assume what day or hour it is: if the answer turns on a schedule "
            "(which night a place is good, whether something is open), ask "
            "what day they mean or answer for the week rather than picking a "
            "day yourself."
        )
    return (
        f"For the user it is {_WEEKDAYS[parsed.weekday()]}, "
        f"{parsed.strftime('%d %B %Y')}, {parsed.strftime('%H:%M')} local time. "
        "Use this without being told: a place whose big night is today leads "
        "the answer, one whose night is later in the week is a plan for that "
        "day, and one that is closed or wrong for right now is a skip you say "
        "out loud rather than a name you omit."
    )


def _render_movement_context(state: AgentState) -> str:
    """Render the `{movement_context}` slot — the turn's resolved search scope.

    Reads the scope the `resolve_location` node folded onto `working_location`
    (ADR-084). When the request carried no `movement_profile`, a config
    fallback kept the radius math working — but the slot flags that so the
    agent asks / caveats rather than asserting a distance confidently.
    """
    _am, _reach, is_fallback = _mobility_profile(state.get("movement_profile"))
    working = state.get("working_location")
    # Same defensive coercion as `_render_location_context`: the gate
    # may skip the resolver and leave the inherit sentinel string in
    # state, which is truthy but not a dict.
    if not isinstance(working, dict) or not working:
        if is_fallback:
            return (
                "No movement profile for this turn. If distance is load-bearing "
                "for the answer, ask the user how they will get around before "
                "scaling any distance — do not assume."
            )
        return "No working location resolved this turn, so no search scope."

    mode = working.get("effective_mode")
    tier = working.get("scope_tier")
    shape = working.get("scope_shape")
    radius_km = (working.get("search_radius_m") or 0.0) / 1000.0
    parts = [
        f"Search scope for this turn: {shape}, about {radius_km:.1f} km, "
        f"by {mode} ({tier} range). Scale any distance reasoning to this — a "
        "per-turn signal overrides the user's default for this turn only."
    ]
    corridor = working.get("corridor")
    if shape == "corridor" and corridor:
        parts.append(
            f"The user is looking along the route toward {corridor.get('name')} "
            "— reason about places on the way, not a circle around one point."
        )
    itinerary = working.get("itinerary") or []
    if shape == "itinerary" and len(itinerary) >= 2:
        parts.append(
            "The user is travelling a multi-stop route — distances within "
            "each stop scale to the mode above, but the stops themselves are "
            "a journey, not a radius."
        )
    if is_fallback:
        parts.append(
            "This used a neutral fallback, not the user's own profile — if "
            "distance is load-bearing, ask how they will get around rather "
            "than asserting it."
        )
    return " ".join(parts)


# Splits the agent prompt into a static, cacheable head and a per-turn
# dynamic tail (ADR-100). Everything before the marker is identical for every
# user and turn, so it caches across requests (Anthropic ephemeral prefix);
# the per-turn slots (location, movement, taste, memory) live after it and are
# never part of the cached prefix. The marker is stripped and never reaches
# the model.
_DYNAMIC_CONTEXT_MARKER = "<<<DYNAMIC_CONTEXT>>>"


def _render_system_prompt(state: AgentState) -> tuple[str, str]:
    """Render the agent prompt as `(static_head, dynamic_tail)`.

    The static head carries no slots and is byte-identical for every user
    and turn — that is what makes it a cacheable Anthropic prefix (ADR-100).
    Only the dynamic tail is `.format(...)`-substituted with the per-turn
    summaries. Template slots are validated at `_load_prompts()` boot time
    (FR-018a), so the substitution is safe.

    `taste_profile_summary` and `memory_summary` are derived from user
    content (memory facts the user typed; signals against places the
    user named). They are wrapped in `trust="low"` blocks so the model
    treats them as data, not instruction (see `prompt_safety`).

    Defensive: if the template carries no split marker, the whole prompt is
    treated as the dynamic tail (single uncached block) so rendering can
    never leave a raw `{slot}` in front of the model.
    """
    from kebi.core.prompt_safety import wrap_untrusted

    template = get_config().prompts["agent"].content
    static_head, marker, dynamic_tail = template.partition(_DYNAMIC_CONTEXT_MARKER)
    slots = dict(
        location_context=_render_location_context(state),
        time_context=_render_time_context(state),
        movement_context=_render_movement_context(state),
        taste_profile_summary=wrap_untrusted(
            state.get("taste_profile_summary"), "taste_profile"
        ),
        memory_summary=wrap_untrusted(state.get("memory_summary"), "user_memories"),
    )
    if not marker:
        return "", template.format(**slots)
    return static_head.strip(), dynamic_tail.format(**slots).strip()


def make_agent_node(llm: Any, tools: list[Any]) -> Any:
    """Return an agent-node callable bound to `llm` and `tools`.

    The node renders the system prompt with per-turn summaries, calls
    `llm.bind_tools(tools).ainvoke(...)`, appends the response to
    `messages`, increments `steps_taken`, and emits one user-visible
    `agent.tool_decision` reasoning step per LLM call (feature 028 M5).

    The persisted reasoning step's `summary` carries `AIMessage.content`
    truncated to 200 chars. When `content` is empty (tool-call-only
    response), a plain fallback summary is used. The SSE lifecycle frames
    (via `stream_emit`) carry an `active` frame before the LLM call and a
    `done` frame with the full, untruncated text after it.

    Visibility is gated on tool calls (ADR-102): a response that carries
    tool calls is intermediate narration the client shows; a response with
    no tool calls IS the terminal answer, whose content is byte-identical
    to the `message` frame, so the step is marked `debug` to keep it off
    the user-visible trace (it still rides SSE for tracing). Otherwise the
    thinking panel would render the whole answer, then the answer again.
    """
    bound = llm.bind_tools(tools, parallel_tool_calls=False)

    async def agent_node(state: AgentState) -> dict[str, Any]:
        static_head, dynamic_tail = _render_system_prompt(state)
        if get_config().agent.prompt_caching_enabled and static_head:
            # Two content blocks: the static head carries the cache breakpoint
            # so the tools + static prefix cache across turns/users (ADR-100),
            # while the per-turn dynamic tail stays outside the cached prefix.
            system_content: str | list[str | dict[str, Any]] = [
                {
                    "type": "text",
                    "text": static_head,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": dynamic_tail},
            ]
        else:
            system_content = f"{static_head}\n\n{dynamic_tail}".strip()
        system = SystemMessage(content=system_content)
        max_hist = get_config().agent.max_history_messages
        trimmed = state["messages"][-max_hist:]
        trimmed, stripped = _strip_orphaned_tool_results(trimmed)
        if stripped:
            logger.warning(
                "Stripped %d ToolMessage(s) whose AIMessage was trimmed from history",
                stripped,
            )
        sanitized, dropped = _sanitize_orphaned_tool_calls(trimmed)
        if dropped:
            logger.warning(
                "Injected %d placeholder ToolMessage(s) for orphaned tool_use blocks",
                dropped,
            )
        compacted_msgs, compacted = _compact_old_tool_results(
            sanitized, keep_recent=get_config().agent.tool_result_window
        )
        if compacted:
            logger.info("Compacted %d older ToolMessage payload(s)", compacted)
        conversation = [system, *compacted_msgs]

        user_id = state.get("user_id")

        def _orchestrator_span() -> TracingSpan:
            return feature_span(
                "agent.orchestrator",
                "agent",
                user_id=user_id,
                role="orchestrator",
            )

        # SSE lifecycle: announce the step before the LLM call so a streaming
        # client can show a skeleton while the orchestrator thinks. The id is
        # keyed by `steps_taken` (read before the +1 write) so the matching
        # `done` frame below carries the same id across both branches.
        # The orchestrator's own thinking is debug on BOTH frames (ADR-103):
        # it's either between-tool monologue (the tool steps already narrate the
        # work) or the terminal answer (identical to the message frame). Keeping
        # the visibility identical across active+done is also required — the
        # client keys on `id`, so a step that flips user→debug never resolves.
        step_id = f"agent.tool_decision#{state.get('steps_taken', 0)}"
        started = emit_step_active(
            step_id,
            "agent.tool_decision",
            title="thinking",
            source="agent",
            visibility="debug",
        )

        try:
            ai_msg = await _invoke_llm_with_retry(
                bound,
                conversation,
                make_span=_orchestrator_span,
                extract_usage=_ai_message_usage,
            )
        except Exception as exc:
            logger.exception("agent_node failed after retries: %s", exc)
            # Summary marker — the per-attempt spans above already carry
            # the failed attempts; this one records that the retry budget
            # was exhausted (distinct event in Langfuse views).
            feature_span("agent.orchestrator.exhausted", "agent", user_id=user_id).end(
                output={"error_type": "llm_retry_exhausted"}, level="ERROR"
            )
            error_msg = AIMessage(
                content=(
                    "I hit a temporary connection issue talking to my language "
                    "model. Please try again in a moment."
                )
            )
            step = ReasoningStep(
                step="agent.tool_decision",
                title="thinking",
                summary="I hit a brief connection issue — give that another try.",
                source="agent",
                visibility="debug",
                duration_ms=0.0,
            )
            emit_step_done(step_id, step, started=started)
            return {
                "messages": [error_msg],
                "error_count": state.get("error_count", 0) + 1,
                "steps_taken": state.get("steps_taken", 0) + 1,
                "reasoning_steps": (state.get("reasoning_steps") or []) + [step],
            }

        full_text = extract_text_content(getattr(ai_msg, "content", None)).strip()

        summary_source = full_text if full_text else _DIRECT_RESPONSE_FALLBACK

        # The streamed `done` frame keeps the full, untruncated text; the
        # persisted step truncates to 200 chars so checkpointer history stays
        # bounded. Debug-only (see the active frame above) — the client filters
        # it, so the answer never duplicates and the thinking row never lingers.
        done_step = ReasoningStep(
            step="agent.tool_decision",
            title="thinking",
            summary=summary_source,
            source="agent",
            visibility="debug",
            duration_ms=0.0,
        )
        emit_step_done(step_id, done_step, started=started)

        step = done_step.model_copy(update={"summary": summary_source[:200]})
        existing_steps = state.get("reasoning_steps") or []
        return {
            "messages": [ai_msg],
            "steps_taken": state.get("steps_taken", 0) + 1,
            # Tool nodes own incrementing tool_calls_used — the agent node
            # just preserves whatever they wrote.
            "tool_calls_used": state.get("tool_calls_used", 0),
            "reasoning_steps": existing_steps + [step],
        }

    return agent_node


def should_continue(state: AgentState) -> str:
    """Route from the agent node.

    Precedence (FR-026):
      tool_calls_used >= max_tool_calls → "fallback"  (own message branch)
      error_count     >= max_errors     → "fallback"
      steps_taken     >= max_steps      → "fallback"
      last message has tool_calls       → "tools"
      trip guard fires (ADR-150)        → "trip_guard"
      otherwise                          → "end"

    The tool-call cap is checked first so the cap-hit case owns its
    branch in `fallback_node` rather than degrading to the generic
    "something went wrong" message that `max_errors` / `max_steps` use.
    """
    cfg = get_config().agent
    if state.get("tool_calls_used", 0) >= _effective_max_tool_calls(state):
        return NODE_FALLBACK
    if state.get("error_count", 0) >= cfg.max_errors:
        return NODE_FALLBACK
    if state.get("steps_taken", 0) >= cfg.max_steps:
        return NODE_FALLBACK

    messages = state.get("messages") or []
    if not messages:
        return "end"
    last = messages[-1]
    tool_calls = getattr(last, "tool_calls", None)
    if tool_calls:
        return NODE_TOOLS
    if _trip_guard_should_fire(state):
        return NODE_TRIP_GUARD
    return "end"


def _effective_max_tool_calls(state: AgentState) -> int:
    """The turn's tool budget — trip-sized on an itinerary turn (ADR-150).

    A three-stop trip legitimately spends more calls than a single-city
    question: the retrieval pair, per-stop suggestions, and the guard's
    verification round. The flat cap was observed collapsing exactly the
    turns the itinerary machinery exists for into the cap-hit fallback.
    """
    cfg = get_config().agent
    working = state.get("working_location")
    if isinstance(working, dict) and working.get("scope_shape") == "itinerary":
        return max(cfg.max_tool_calls, cfg.itinerary.max_tool_calls)
    return cfg.max_tool_calls


# --- Trip guard (ADR-150) ---------------------------------------------------

# Prefix marking the guard's injected message. The first line carries the
# targeted stop (`[trip guard] target: <stop>`), which is what bounds the
# guard: each stop is targeted at most once per turn, so a stop whose
# verification came back empty is not retried forever.
_TRIP_GUARD_MARKER = "[trip guard]"


def _trip_uncovered_stops(state: AgentState) -> list[str]:
    """Stops of this turn's itinerary that no place-tool candidate covers.

    Coverage is read off the candidates' own `segment` labels in the turn's
    tool payloads — a stop nothing was retrieved for appears in no label. A
    turn that is not an itinerary has no stops to cover.
    """
    working = state.get("working_location")
    if not isinstance(working, dict) or working.get("scope_shape") != "itinerary":
        return []
    stops = [
        name
        for anchor in working.get("itinerary") or []
        if isinstance(anchor, dict)
        and isinstance(name := anchor.get("name"), str)
        and name
    ]
    if len(stops) < 2:
        return []
    covered: set[str] = set()
    for result in state.get("tool_payloads") or []:
        payload = result.get("payload")
        if not isinstance(payload, dict):
            continue
        for candidate in payload.get("candidates") or []:
            if isinstance(candidate, dict) and candidate.get("segment"):
                covered.add(candidate["segment"])
    return [s for s in stops if s not in covered]


def _trip_guard_targeted_stops(messages: list[BaseMessage]) -> set[str]:
    """Stops the guard already targeted in the current turn.

    Read back from the guard's own marker messages (first line
    `[trip guard] target: <stop>`), scanning only after the turn's real
    user message. This is the loop bound: a stop whose verification came
    back empty stays uncovered but is never targeted twice.
    """
    targeted: set[str] = set()
    for msg in reversed(messages):
        text = extract_text_content(msg.content)
        if text.startswith(_TRIP_GUARD_MARKER):
            first_line = text.splitlines()[0]
            stop = first_line.removeprefix(_TRIP_GUARD_MARKER).strip()
            stop = stop.removeprefix("target:").strip()
            if stop:
                targeted.add(stop)
        elif isinstance(msg, HumanMessage):
            break
    return targeted


def _trip_guard_next_stop(state: AgentState) -> str | None:
    """The next uncovered, not-yet-targeted stop, or None when done."""
    uncovered = _trip_uncovered_stops(state)
    if not uncovered:
        return None
    targeted = _trip_guard_targeted_stops(state.get("messages") or [])
    for stop in uncovered:
        if stop not in targeted:
            return stop
    return None


# The polish mode's pseudo-target in the marker line — distinct from any
# stop name, so the targeted-once bookkeeping covers both guard modes.
_TRIP_POLISH_TARGET = "polish"


def _trip_missing_saves(state: AgentState) -> list[str]:
    """The user's retrieved saves that the draft answer never names.

    Deterministic prose check: a save surfaced by retrieval is known by its
    names and aliases, and a draft that contains none of them has silently
    dropped a place the user owns — the single biggest observed variance in
    trip answers (a saved Mount Fuji vanished between two otherwise-good
    runs). Matching is case-folded substring over every known spelling, so
    a mention via alias or display form counts.
    """
    working = state.get("working_location")
    if not isinstance(working, dict) or working.get("scope_shape") != "itinerary":
        return []
    messages = state.get("messages") or []
    if not messages:
        return []
    draft = extract_text_content(messages[-1].content).casefold()
    if not draft:
        return []

    from kebi.core.places._place_utils import display_place_name

    missing: list[str] = []
    seen: set[str] = set()
    for result in state.get("tool_payloads") or []:
        payload = result.get("payload")
        if not isinstance(payload, dict):
            continue
        for candidate in payload.get("candidates") or []:
            if not isinstance(candidate, dict) or not candidate.get("user_data"):
                continue
            place = candidate.get("place") or {}
            name = place.get("place_name")
            if not isinstance(name, str) or not name or name in seen:
                continue
            seen.add(name)
            spellings = {name, display_place_name(name)}
            for alias in place.get("place_name_aliases") or []:
                value = alias.get("value") if isinstance(alias, dict) else None
                if isinstance(value, str) and value:
                    spellings.add(value)
            if not any(s.casefold() in draft for s in spellings if s):
                missing.append(display_place_name(name))
    return missing


def _trip_guard_should_fire(state: AgentState) -> bool:
    """Whether to intercept a final trip answer that skipped verification.

    Prompt instruction alone proved ~partial, and the live failure that
    motivated this turned out to be structural anyway: the orchestrator
    was being told to call a tool its entitlement tier had not bound. The
    guard makes the rule routing, not persuasion: a trip answer may not
    finalize while a stop has zero retrieved candidates and budget remains.
    One stop is targeted per firing (parallel injected Commands collide on
    the plain payload channel), each stop at most once per turn — so the
    worst case is one verification round per stop, then the answer stands.

    The second mode is the polish pass: every stop covered, but the draft
    silently dropped a save the user owns. That costs no tool call — one
    text-only round naming the dropped saves — and runs at most once.
    """
    cfg = get_config().agent
    if state.get("steps_taken", 0) >= cfg.max_steps - 1:
        return False
    targeted = _trip_guard_targeted_stops(state.get("messages") or [])
    if _trip_guard_next_stop(state) is not None and state.get(
        "tool_calls_used", 0
    ) < _effective_max_tool_calls(state):
        return True
    return _TRIP_POLISH_TARGET not in targeted and bool(_trip_missing_saves(state))


def trip_guard_node(state: AgentState) -> dict[str, Any]:
    """Fetch verified picks for the uncovered stops, then have the model
    rewrite (ADR-150).

    Asking the model to make the call failed on both orchestrator tiers —
    each preferred the honest-empty line over the tool. So the guard does
    not ask: it appends its own `suggest_places` call per uncovered stop
    (bounded by the remaining budget) and routes through the tool node. No
    `names` are supplied, which is the tool's namer fallback (ADR-141): a
    dedicated model names the picks, the provider verifies each one, and
    the survivors come back tappable and persisted. The user's taste rides
    the query, so what surfaces is taste-filtered general knowledge — the
    defensible version of the model knowing things. The message text tells
    the model what is happening and what its rewrite must do; the marker
    doubles as the fired-once flag.
    """
    stop = _trip_guard_next_stop(state)
    if stop is None or state.get("tool_calls_used", 0) >= _effective_max_tool_calls(
        state
    ):
        return _trip_polish_update(state)
    logger.info("trip guard fired: fetching verified picks for %s", stop)

    # Taste LEADS the query, it doesn't trail it: "places worth a stop, for
    # someone who likes X" returned the generic tourist canon with taste as
    # an afterthought. Named first, the taste values are what the namer
    # actually picks for — the coffee city surfaces its roaster, not only
    # its castle.
    taste = [
        v.replace("_", " ") for v in (state.get("taste_values") or []) if v.strip()
    ]
    query = (
        f"for {stop}: one standout spot for EACH of their tastes "
        f"({', '.join(taste)}), plus the one or two sights genuinely worth "
        "their time"
        if taste
        else f"the places actually worth a stop in {stop}"
    )
    tool_calls = [
        {
            "name": "suggest_places",
            "args": {"query": query, "city": stop},
            "id": f"trip_guard_{stop.lower().replace(' ', '_')}",
        }
    ]
    text = (
        f"{_TRIP_GUARD_MARKER} target: {stop}\n"
        f"Your draft named nothing verified for {stop}. Fetching verified "
        "picks now. Rewrite the FULL answer when they arrive: use only "
        "verified candidates for that stop (taste said out loud), drop "
        "every unverified name, and never mention this step."
    )
    return {"messages": [AIMessage(content=text, tool_calls=tool_calls)]}


def _trip_polish_update(state: AgentState) -> dict[str, Any]:
    """Text-only guard round: the draft dropped saves the user owns.

    No tool call — the missing places are already retrieved and sitting in
    the conversation's tool results; the model just failed to spend them.
    The nudge names them and restates the trip-prose rules that land
    intermittently, because this is the one deterministic hook the server
    has into the draft's completeness.
    """
    missing = _trip_missing_saves(state)
    logger.info("trip guard polish: draft omitted saves %s", missing)
    names = ", ".join(missing) if missing else "(none)"
    text = (
        f"{_TRIP_GUARD_MARKER} target: {_TRIP_POLISH_TARGET}\n"
        f"Your draft silently dropped saves the user owns: {names}. They "
        "are already in your tool results. Rewrite the FULL answer: every "
        "save earns its line (even a skip, with the reason), a taste-led "
        'pick says "based on your taste", two same-kind saves get a '
        "pick-one call with the reason, and the close folds the nights "
        "question into an offer of the next piece of work. Never mention "
        "this step."
    )
    # HumanMessage, not AIMessage: a text-only assistant message at the end
    # of the conversation reads as prefill to the Anthropic API (400). The
    # verification mode's AIMessage is fine because its tool calls are
    # followed by tool results.
    return {"messages": [HumanMessage(content=text)]}


# --- Location resolution ---------------------------------------------------

# Single-word signals that a turn is location- or movement-relevant. The gate
# is a *performance optimization, not a correctness gate*: a false positive
# (e.g. "guilt trip") costs only one extra resolver call, and the resolver
# itself classifies by intent. So this errs toward over-triggering — do not
# hand-tune it to suppress false positives. Only broad prepositions ("in",
# "to"), which would fire on nearly every message, are deliberately excluded.
_LOCATION_GATE_KEYWORDS = frozenset(
    {
        "near",
        "nearby",
        "around",
        "here",
        "where",
        "neighborhood",
        "neighbourhood",
        "area",
        "walk",
        "walking",
        "downtown",
        "city",
        "distance",
        "local",
        "locally",
        # Travel-intent words — "visiting X", "trip to X", "on vacation in
        # X". A lowercased place name (e.g. "hadar haifa") does not trip the
        # proper-noun heuristic; these keywords give the gate a second
        # chance to send the turn through the resolver. Note: time-only
        # words ("week", "tomorrow", "tonight") are deliberately NOT here
        # — they fire on saved-history questions like "what did I save
        # last week". Future-tense travel intent is matched via the
        # bigram phrases below instead ("next week", "going to", ...).
        "visit",
        "visiting",
        "trip",
        "travelling",
        "traveling",
        "vacation",
        "holiday",
        # Movement words (ADR-084) — a movement-only turn still needs scope.
        "drive",
        "driving",
        "car",
        "transit",
        "bus",
        "train",
        "bike",
        "biking",
        "cycling",
        "ride",
        "rideshare",
        "commute",
    }
)
# Phrases that often precede a location shift or movement signal even with no
# capitalisation.
_LOCATION_GATE_PHRASES = (
    "what about",
    "how about",
    "near me",
    "around here",
    "on my way",
    "day trip",
    # "I'm in <place>", "I am in <place>", "going to", "heading to" — common
    # travel-intent phrasings that don't otherwise capitalise their place
    # name. Cheap heuristic; the resolver still has the final say.
    "i'm in ",
    "i am in ",
    "going to ",
    "heading to ",
    "off to ",
    "next week",
    "next month",
)
# Words that mark a turn as clearly location-free: greetings, acknowledgements,
# a recall of saved history (the taste / library surface, not a place query), or
# a meta / help question. None of these need a working location, so they skip the
# resolver even though the gate otherwise resolves by default (see the gate
# docstring — a lowercased place name like "da nang cafes" carries no keyword and
# must NOT skip and answer for a stale carried city). Accepted edge: a genuine
# place query that also contains a marker word ("save me a spot in da nang") skips.
_LOCATION_FREE_MARKERS = frozenset(
    {
        "hi",
        "hello",
        "hey",
        "yo",
        "thanks",
        "thank",
        "thx",
        "ty",
        "cheers",
        "save",
        "saved",
        "stash",
        "library",
        "bookmarked",
        "history",
        "help",
    }
)


def _last_human_text(messages: list[Any]) -> str:
    """Return the text of the most recent HumanMessage, or ''."""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            return extract_text_content(msg.content).strip()
    return ""


def _needs_location_resolution(state: AgentState) -> str:
    """Gate routing function — decide whether to run `resolve_location`.

    Returns `"resolve"` or `"skip"`. Biased toward `"resolve"`: a false skip
    misses a real location shift (the carried location goes stale), which is
    worse than a wasted resolver call on a location-free turn. Only clearly
    location-free turns — greetings, meta questions — are skipped.
    """
    text = _last_human_text(state.get("messages") or [])
    if not text:
        return "skip"
    lowered = text.lower()
    if any(phrase in lowered for phrase in _LOCATION_GATE_PHRASES):
        return "resolve"
    if any(word in _LOCATION_GATE_KEYWORDS for word in re.findall(r"[a-z]+", lowered)):
        return "resolve"
    # A capitalised token that is not the first word is likely a proper noun
    # (a place name) — resolve to be safe.
    tokens = text.split()
    for token in tokens[1:]:
        cleaned = token.strip(".,!?;:'\"()")
        if len(cleaned) > 1 and cleaned[0].isupper() and not cleaned.isupper():
            return "resolve"
    # Resolve by default. A lowercased place name with no travel keyword
    # ("da nang cafes", "what's good in da nang") reaches here, and skipping it
    # would let a stale carried location answer for the wrong city. Only clearly
    # location-free turns — greetings, acknowledgements, saved-history recall,
    # meta / help questions — skip.
    words = set(re.findall(r"[a-z']+", lowered))
    if words & _LOCATION_FREE_MARKERS:
        return "skip"
    return "resolve"


def _format_history_for_resolver(messages: list[BaseMessage]) -> str:
    """Render recent turns as plain `role: text` lines for the resolver prompt.

    Excludes the final HumanMessage (passed separately as the current
    message) and caps at the last few exchanges.
    """
    relevant = [m for m in messages if isinstance(m, HumanMessage | AIMessage)]
    history = relevant[:-1][-8:] if relevant else []
    lines: list[str] = []
    for msg in history:
        role = "user" if isinstance(msg, HumanMessage) else "assistant"
        text = extract_text_content(msg.content).strip()
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines)


def _mobility_profile(
    profile: dict[str, Any] | None,
) -> tuple[list[str], str, bool]:
    """Return ``(available_modes, reach, is_fallback)``.

    A request without a `movement_profile` uses the config fallback — the
    radius math stays functional, but `is_fallback` lets the agent prompt know
    movement is unresolved (it should ask / caveat rather than assert).
    """
    if profile:
        return (
            [str(m) for m in (profile.get("available_modes") or [])],
            str(profile.get("reach") or "normal"),
            False,
        )
    fb = get_config().movement.fallback
    return list(fb.available_modes), fb.reach, True


def _render_mobility_profile(state: AgentState) -> str:
    """Render the `{mobility_profile}` slot for the resolver prompt."""
    available, reach, is_fallback = _mobility_profile(state.get("movement_profile"))
    note = (
        " (the request carried no profile — these are neutral fallbacks)"
        if is_fallback
        else ""
    )
    return f"Capabilities: {', '.join(available) or 'unknown'}. Reach: {reach}.{note}"


def _carried_working_location(state: AgentState) -> dict[str, Any] | None:
    """Prior turn's working location as a dict, or None when there is none.

    `build_turn_payload` seeds `working_location` with the `LOCATION_INHERIT`
    sentinel every turn so carry-forward is explicit. On a brand-new thread
    LangGraph's reducer channel stores that first value *as-is* — the reducer
    only combines *subsequent* writes — so the raw sentinel string can be the
    value when `resolve_location` reads state on the very first turn. Anything
    that is not a dict (the sentinel, or a cleared `None`) means "no carried
    location", so coerce it to `None` here and everywhere the resolver reads
    the prior location.
    """
    wl = state.get("working_location")
    return wl if isinstance(wl, dict) else None


def _distance_from_previous_km(state: AgentState) -> float | None:
    """Distance between user_actual and previous working location, in km.

    `None` when either side is missing (first turn, request without
    coords, prior turn produced no working location). The resolver
    prompt's **traveled** branch uses this to decide whether a generic
    follow-up message ("what's around here?") should keep carrying the
    prior place or re-anchor to the user's new actual location.
    """
    user_loc = state.get("user_location") or {}
    prev = _carried_working_location(state) or {}
    user_lat = _coerce_coord(user_loc.get("lat"))
    user_lng = _coerce_coord(user_loc.get("lng"))
    prev_lat = _coerce_coord(prev.get("lat"))
    prev_lng = _coerce_coord(prev.get("lng"))
    if None in (user_lat, user_lng, prev_lat, prev_lng):
        return None
    # mypy: the None-check above narrows all four to float
    metres = haversine_m(
        user_lat,  # type: ignore[arg-type]
        user_lng,  # type: ignore[arg-type]
        prev_lat,  # type: ignore[arg-type]
        prev_lng,  # type: ignore[arg-type]
    )
    return metres / 1000.0


def _render_distance_from_previous(state: AgentState) -> str:
    """Human-readable rendering of the {distance_from_previous} slot."""
    prev = _carried_working_location(state) or {}
    user_loc = state.get("user_location") or {}
    if not prev:
        return "first turn — no previous working location"
    if not user_loc:
        return "actual location is unknown this turn"
    km = _distance_from_previous_km(state)
    if km is None:
        return "actual location is unknown this turn"
    # Round to a sensible precision — exact metres are noise to the LLM.
    if km < 1:
        return "actual location is at (or essentially at) the previous working location"
    if km < 10:
        return f"actual location is ~{km:.1f} km from the previous working location"
    return f"actual location is ~{int(round(km))} km from the previous working location"


def _render_resolver_prompt(state: AgentState) -> str:
    """Format the location-resolver prompt with this turn's inputs."""
    template = get_config().prompts["location_resolver"].content
    messages = state.get("messages") or []
    return template.format(
        current_message=_last_human_text(messages) or "(empty)",
        conversation_history=(
            _format_history_for_resolver(messages) or "(no prior messages)"
        ),
        user_actual_location=json.dumps(state.get("user_location")),
        previous_working_location=json.dumps(_carried_working_location(state)),
        distance_from_previous=_render_distance_from_previous(state),
        mobility_profile=_render_mobility_profile(state),
    )


def _coerce_coord(value: Any) -> float | None:
    """Best-effort float for a lat/lng pulled from a state dict."""
    if isinstance(value, int | float):
        return float(value)
    return None


def _with_area_icons(
    working: WorkingLocation, resolution: LocationResolution
) -> WorkingLocation:
    """Fill a carried location's missing area icons from this turn's resolution.

    A carried location arrives with the icons its own turn resolved, and those
    win — the same area keeps the same emoji for the whole conversation rather
    than flickering as the model re-picks. This only covers the gap: a location
    checkpointed before icons existed, or a turn whose resolution came back
    without one.
    """
    filled: dict[str, str] = {}
    if not working.city_icon and resolution.city_icon:
        filled["city_icon"] = resolution.city_icon
    if not working.neighborhood_icon and resolution.neighborhood_icon:
        filled["neighborhood_icon"] = resolution.neighborhood_icon
    return working.model_copy(update=filled) if filled else working


async def _build_working_location(
    resolution: LocationResolution,
    user_location: dict[str, Any] | None,
    prior_working_location: dict[str, Any] | None,
    geocoding_client: Any,
) -> WorkingLocation | None:
    """Turn a resolver decision into a complete `WorkingLocation`, or `None`.

    Coordinates are derived deterministically — never transcribed by the LLM:
      - `carried`        → reuse the prior turn's working location verbatim.
      - `user_actual`    → take the user's actual GPS coords from the request
                           and reverse-geocode them for the place names.
      - `explicit_query` → forward-geocode the place the resolver named.

    Returns `None` when the location cannot be completed; the caller maps
    that to a clarification. Both `explicit_query` and `user_actual` need
    country + city + coords; `neighborhood` is optional — a bare city the
    user names ("what about Chiang Mai") is a complete working location, and
    reverse geocoding often cannot name a neighbourhood anyway. Genuinely
    ambiguous names (Cambridge UK vs MA) are caught upstream by the resolver,
    not by demanding a neighbourhood here.
    """
    source = resolution.source

    if source == "carried":
        if prior_working_location:
            try:
                return _with_area_icons(
                    WorkingLocation(**prior_working_location), resolution
                )
            except (TypeError, ValueError):
                return None
        # Resolver said "carry" but there is nothing carried (e.g. first
        # turn) — fall back to the user's actual location.
        source = "user_actual"

    if source == "user_actual":
        if not user_location:
            return None
        lat = _coerce_coord(user_location.get("lat"))
        lng = _coerce_coord(user_location.get("lng"))
        if lat is None or lng is None:
            return None
        rev = await geocoding_client.reverse(lat=lat, lng=lng)
        if rev is None or not rev.country or not rev.city:
            return None
        return WorkingLocation(
            country=rev.country,
            city=rev.city,
            neighborhood=rev.neighborhood,
            country_code=rev.country_code,
            lat=lat,
            lng=lng,
            density=density_class(rev.place_type),
            bbox=rev.bbox,
            city_icon=resolution.city_icon,
            neighborhood_icon=resolution.neighborhood_icon,
        )

    # explicit_query — a place the resolver named; geocode it.
    country = resolution.country
    city = resolution.city
    neighborhood = resolution.neighborhood
    if not country or not city:
        # A user-named place must at least resolve to a city; neighborhood
        # is optional (a bare city is a complete working location).
        return None
    geo = await geocoding_client.forward(
        country=country, city=city, neighborhood=neighborhood
    )
    if geo is None:
        return None
    return WorkingLocation(
        country=country,
        city=city,
        neighborhood=neighborhood,
        country_code=geo.country_code,
        lat=geo.lat,
        lng=geo.lng,
        density=density_class(geo.place_type),
        bbox=geo.bbox,
        city_icon=resolution.city_icon,
        neighborhood_icon=resolution.neighborhood_icon,
    )


async def _resolve_corridor(
    destination: str | None,
    working: WorkingLocation,
    geocoding_client: Any,
) -> CorridorTarget | None:
    """Eagerly geocode a corridor destination, scoped to the working city.

    Returns `None` when there is no destination name or it cannot be
    geocoded. The resolver is instructed to flag implicit anchors ("home",
    "work" — kebi stores no user addresses) as needing clarification before
    they ever reach here; this is the second line of defence for a named
    place that simply does not geocode. The caller maps `None` to a
    clarification ask — never a silent fallback to an area search.
    """
    name = (destination or "").strip()
    if not name:
        return None
    query = ", ".join(p for p in (name, working.city, working.country) if p)
    geo = await geocoding_client.search(query=query)
    if geo is None:
        return None
    return CorridorTarget(name=name, lat=geo.lat, lng=geo.lng)


async def _resolve_itinerary(
    stops: list[str],
    geocoding_client: Any,
) -> list[ItineraryAnchor]:
    """Geocode the resolver's ordered stop names into itinerary anchors.

    Stops arrive as "City, Country" strings (the resolver prompt requires
    the country so bare names like "Hue" can't mis-anchor — the same lesson
    as ADR-126). A stop that fails to geocode is dropped with a log rather
    than failing the turn: the trip is still answerable from the stops that
    resolved, and the anchor label keeps only the city half so the agent
    quotes "Hue", not "Hue, Vietnam". Capped at `agent.itinerary.max_stops`.
    """
    anchors: list[ItineraryAnchor] = []
    for stop in stops[: get_config().agent.itinerary.max_stops]:
        name = stop.strip()
        if not name:
            continue
        try:
            geo = await geocoding_client.search(query=name)
        except GeocodingError:
            geo = None
        if geo is None:
            logger.warning("itinerary stop failed to geocode, dropping: %r", name)
            continue
        anchors.append(
            ItineraryAnchor(
                name=name.split(",")[0].strip() or name,
                lat=geo.lat,
                lng=geo.lng,
                city=geo.city,
                country=geo.country,
                country_code=geo.country_code,
            )
        )
    return anchors


async def _resolve_search_scope(
    working: WorkingLocation,
    resolution: LocationResolution,
    movement_profile: dict[str, Any] | None,
    geocoding_client: Any,
) -> WorkingLocation | None:
    """Fold the resolved movement scope onto a working location (ADR-084 /
    ADR-085).

    Picks the effective mode (resolver classification, else a deterministic
    fallback to the first listed capability), derives `search_radius_m` from
    config, and — for a corridor — eagerly geocodes the destination. Returns
    `None` only when the turn is a corridor whose destination cannot be
    resolved; the caller maps that to a clarification ask.
    """
    available, reach, _is_fallback = _mobility_profile(movement_profile)
    fallback_mode = available[0] if available else "transit"
    effective_mode = resolution.effective_mode or fallback_mode

    corridor: CorridorTarget | None = None
    if resolution.scope_shape == "corridor":
        corridor = await _resolve_corridor(
            resolution.corridor_destination, working, geocoding_client
        )
        if corridor is None:
            return None

    # An itinerary that loses too many stops to geocoding degrades to a
    # plain area turn around the primary anchor — a worse answer, never a
    # failed one (ADR-148). The shape only survives with >= 2 real anchors,
    # so downstream fan-out can rely on at least one leg existing.
    scope_shape: str = resolution.scope_shape
    itinerary: list[ItineraryAnchor] | None = None
    if resolution.scope_shape == "itinerary":
        anchors = await _resolve_itinerary(resolution.itinerary_stops, geocoding_client)
        if len(anchors) >= 2:
            itinerary = anchors
        else:
            logger.warning(
                "itinerary resolved %d/%d stops; degrading to area scope",
                len(anchors),
                len(resolution.itinerary_stops),
            )
            scope_shape = "area"

    radius = resolve_radius(
        effective_mode,
        resolution.scope_tier,
        reach,
        working.density,
        get_config().movement,
    )
    return working.model_copy(
        update={
            "effective_mode": effective_mode,
            "scope_tier": resolution.scope_tier,
            "scope_shape": scope_shape,
            "search_radius_m": radius,
            "corridor": corridor,
            "itinerary": itinerary,
        }
    )


# One location resolution per turn, so a single stable lifecycle id. The
# `active` frame is emitted at node entry (`agent.location` step name); the
# `done` frame reuses this id with the resolved/clarify step below — the
# frontend keys on the id, not the step name.
_LOCATION_STEP_ID = "agent.location#0"


def _location_clarification_update(
    state: AgentState, reason: str, *, started: float | None = None
) -> dict[str, Any]:
    """State update that clears the working location and asks the user."""
    step = ReasoningStep(
        step="agent.location_clarify",
        title="checking your location",
        summary=reason,
        source="agent",
        visibility="user",
        duration_ms=0.0,
    )
    emit_step_done(_LOCATION_STEP_ID, step, started=started)
    return {
        "working_location": None,
        "location_clarification": reason,
        "reasoning_steps": (state.get("reasoning_steps") or []) + [step],
    }


def _location_not_needed_update(
    state: AgentState, *, started: float | None = None
) -> dict[str, Any]:
    """State update for a turn that is not about a place at all (ADR-145).

    Distinct from a clarification, and the distinction matters: both leave
    `working_location` empty, but a clarification *ends the turn* with a
    question. Before web search existed that was harmless — every question
    kebi could answer was about somewhere — but "where is the World Cup being
    played" has no working location and needs none, and asking the user which
    city they mean is a non-sequitur that burns the turn.

    So the resolver gets to say "not applicable" explicitly rather than the
    node inferring it from an empty result. The turn proceeds with no
    location; every tool already treats that as a valid state.
    """
    step = ReasoningStep(
        step="agent.location_not_needed",
        title="checking your location",
        summary="this one isn't about where you are",
        source="agent",
        # `debug`, not `user`: the client shows user-visible steps as the
        # answer's narration, and "checking your location" on a World Cup
        # question reads as confusion, not work.
        visibility="debug",
        duration_ms=0.0,
    )
    emit_step_done(_LOCATION_STEP_ID, step, started=started)
    return {
        "working_location": None,
        "location_clarification": None,
        "reasoning_steps": (state.get("reasoning_steps") or []) + [step],
    }


def _location_resolved_update(
    state: AgentState, working: WorkingLocation, *, started: float | None = None
) -> dict[str, Any]:
    """State update that records a fully resolved working location."""
    # Scope is invisible in the trace otherwise: a corridor turn that silently
    # degraded to an area search looks identical to a normal one, and the only
    # symptom is an answer confidently naming places in the wrong direction.
    logger.info(
        "resolved scope: shape=%s tier=%s mode=%s radius=%.0fm corridor=%s "
        "itinerary=%s",
        working.scope_shape,
        working.scope_tier,
        working.effective_mode,
        working.search_radius_m,
        working.corridor.name if working.corridor else None,
        [a.name for a in working.itinerary] if working.itinerary else None,
    )
    parts = [working.neighborhood, working.city, working.country]
    place = ", ".join(p for p in parts if p)
    step = ReasoningStep(
        step="agent.location_resolved",
        title="found your location",
        summary=f"around {place}",
        source="agent",
        visibility="user",
        duration_ms=0.0,
    )
    emit_step_done(_LOCATION_STEP_ID, step, started=started)
    return {
        "working_location": working.model_dump(),
        "location_clarification": None,
        "reasoning_steps": (state.get("reasoning_steps") or []) + [step],
    }


_LOCATION_ASK_CITY = (
    "I couldn't work out which location you mean — which city or area is this about?"
)
_LOCATION_ASK_CONFIRM = (
    "I couldn't pin down that location — could you confirm the city?"
)
_CORRIDOR_ASK = "I couldn't work out where your route ends — where are you headed?"


def make_resolve_location_node(resolver_llm: Any, geocoding_client: Any) -> Any:
    """Return the `resolve_location` node bound to its LLM + geocoding client.

    The node resolves the turn's working location: one structured LLM call
    (priority + shift detection + ambiguity), then silent geocoding. It writes
    either a complete `working_location` or a `location_clarification` reason —
    never a partial location. Geocoding or LLM failure fails toward asking the
    user; it never crashes the turn or bumps `error_count`.
    """
    # `include_raw=True` returns a dict {"raw", "parsed", "parsing_error"}
    # so we can pull `usage_metadata` off the raw AIMessage for the
    # Langfuse generation span. The parsed `LocationResolution` is read
    # exactly like before.
    structured = resolver_llm.with_structured_output(
        LocationResolution, include_raw=True
    )

    async def resolve_location_node(state: AgentState) -> dict[str, Any]:
        user_id = state.get("user_id")

        # SSE lifecycle: announce location resolution before the resolver LLM
        # call. The matching `done` frame is emitted by the resolved/clarify
        # update helpers under the same id; `started` carries the timing token.
        started = emit_step_active(
            _LOCATION_STEP_ID,
            "agent.location",
            title="found your location",
            source="agent",
        )

        def _resolver_span() -> TracingSpan:
            return feature_span(
                "agent.location_resolver",
                "agent",
                user_id=user_id,
                role="location_resolver",
            )

        try:
            result = await _invoke_llm_with_retry(
                structured,
                [HumanMessage(content=_render_resolver_prompt(state))],
                make_span=_resolver_span,
                extract_usage=_structured_usage,
            )
        except Exception as exc:
            logger.warning("resolve_location LLM failed: %s", exc)
            return _location_clarification_update(
                state, _LOCATION_ASK_CITY, started=started
            )

        # Defensive: include_raw=True can return a dict with parsing_error
        # set when the model output failed schema validation. Treat that
        # the same as an LLM failure → ask the user.
        if isinstance(result, dict):
            parse_err = result.get("parsing_error")
            parsed = result.get("parsed")
            if parse_err is not None or parsed is None:
                logger.warning("resolve_location parsing_error: %s", parse_err)
                return _location_clarification_update(
                    state, _LOCATION_ASK_CITY, started=started
                )
            resolution: LocationResolution = parsed
        else:
            # Older / cached `with_structured_output` without include_raw —
            # treat the result as the parsed model directly.
            resolution = result

        # Checked before the clarification branch: a question with no place in
        # it must not be turned into "which city do you mean?" (ADR-145).
        if resolution.location_irrelevant:
            return _location_not_needed_update(state, started=started)

        if resolution.needs_clarification or resolution.is_ambiguous:
            reason = (
                resolution.clarification_reason.strip()
                or "the location you mean is ambiguous"
            )
            return _location_clarification_update(state, reason, started=started)

        try:
            working = await _build_working_location(
                resolution,
                state.get("user_location"),
                _carried_working_location(state),
                geocoding_client,
            )
            if working is None:
                return _location_clarification_update(
                    state, _LOCATION_ASK_CONFIRM, started=started
                )
            # Fold in the movement scope (effective mode + tier → radius, and
            # for a corridor, the eagerly geocoded destination).
            working = await _resolve_search_scope(
                working,
                resolution,
                state.get("movement_profile"),
                geocoding_client,
            )
        except GeocodingError as exc:
            logger.warning("resolve_location geocoding failed: %s", exc)
            return _location_clarification_update(
                state, _LOCATION_ASK_CONFIRM, started=started
            )

        if working is None:
            # The point resolved, but a corridor destination did not — ask
            # rather than silently degrading to an area search.
            return _location_clarification_update(state, _CORRIDOR_ASK, started=started)
        return _location_resolved_update(state, working, started=started)

    return resolve_location_node


def fallback_node(state: AgentState) -> dict[str, Any]:
    """Compose a graceful terminal message + reasoning steps.

    Emits one user-visible ReasoningStep (FR-027) plus one debug diagnostic
    step (M9) when applicable (max_tool_calls_detail / max_steps_detail /
    max_errors_detail).

    Precedence matches `should_continue`: the tool-call cap is checked
    first so a cap-hit turn surfaces the dedicated "too vague" message
    rather than the generic apology used for `max_steps`/`max_errors`.
    """
    cfg = get_config().agent
    steps_taken = state.get("steps_taken", 0)
    error_count = state.get("error_count", 0)
    tool_calls_used = state.get("tool_calls_used", 0)

    debug_steps: list[ReasoningStep] = []
    user_message = _FALLBACK_MESSAGE
    if tool_calls_used >= cfg.max_tool_calls:
        error_type = "max_tool_calls"
        summary = (
            "I tried a few angles but couldn't narrow it down — "
            "a bit more detail would help."
        )
        user_message = _TOOL_CAP_MESSAGE
        debug_steps.append(
            ReasoningStep(
                step="max_tool_calls_detail",
                summary=f"exceeded max_tool_calls={cfg.max_tool_calls}",
                source="fallback",
                visibility="debug",
            )
        )
    elif steps_taken >= cfg.max_steps:
        error_type = "max_steps"
        summary = "I got a bit tangled up there — something went wrong on my end."
        debug_steps.append(
            ReasoningStep(
                step="max_steps_detail",
                summary=f"exceeded max_steps={cfg.max_steps}",
                source="fallback",
                visibility="debug",
            )
        )
    elif error_count >= cfg.max_errors:
        error_type = "max_errors"
        summary = "I ran into a few snags — try rephrasing or sharing a bit more."
        debug_steps.append(
            ReasoningStep(
                step="max_errors_detail",
                summary=f"exceeded max_errors={cfg.max_errors}",
                source="fallback",
                visibility="debug",
            )
        )
    else:
        error_type = "max_errors"
        summary = "Something went wrong on my end."

    tracer = get_tracing_client()
    span = tracer.generation("agent_fallback", user_id=state.get("user_id"))
    span.end(output={"error_type": error_type}, level="ERROR")

    user_step = ReasoningStep(
        step="fallback",
        title="wrapping up",
        summary=summary,
        source="fallback",
        visibility="user",
    )
    # Terminal node: the steps are instantaneous, so emit each step's `active`
    # and `done` frames back-to-back (keeps the lifecycle contract uniform —
    # every `done` has a prior `active`). Debug steps ride the SSE too; the
    # frontend filters them by `visibility`.
    for diag in (*debug_steps, user_step):
        diag_id = f"{diag.step}#0"
        diag_started = emit_step_active(
            diag_id,
            diag.step,
            title=diag.title,
            source="fallback",
            visibility=diag.visibility,
        )
        emit_step_done(diag_id, diag, started=diag_started)
    existing_steps = state.get("reasoning_steps") or []
    return {
        "messages": [AIMessage(content=user_message)],
        "reasoning_steps": existing_steps + debug_steps + [user_step],
    }


def _handle_tool_node_error(exc: Exception) -> str:
    """Error handler for ToolNode — logs full traceback, returns JSON string.

    ToolNode's default handler stringifies the exception into plain text,
    which (a) hides the stack trace from the server log and (b) produces
    non-JSON ToolMessage content that the SSE `tool_result` frame cannot
    parse. Logging here captures the real cause and returning JSON keeps
    the client payload structured.
    """
    logger.exception("ToolNode caught exception: %s", exc)
    return json.dumps(
        {
            "error": "tool_invocation_failed",
            "type": type(exc).__name__,
            "message": str(exc)[:500],
        }
    )


def _parse_tool_message_payload(m: ToolMessage) -> dict[str, Any] | None:
    """Parse a ToolMessage's JSON content into a renderable dict.

    Consult-family tools serialise their `ConsultResult` to the
    `ToolMessage.content` JSON string. LangGraph's `ToolNode` returns a
    plain error-string ToolMessage with `status="error"` on argument-
    schema validation failure; that path is surfaced as a structured
    error payload instead of a bare `null`.
    """
    content = m.content if isinstance(m.content, str) else ""
    if getattr(m, "status", None) == "error":
        return {"error": "tool_call_failed", "message": content or "tool error"}
    if not content:
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {"error": "non_json_content", "message": content[:500]}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def finalize_node(state: AgentState) -> dict[str, Any]:
    """Capture tool results and strip tool messages before checkpoint.

    Tool results must not persist in agent history — they bloat every
    future turn with stale per-call JSON the user already received a
    prose summary of. The LangGraph tool_use ↔ tool_result contract
    requires them in-turn, so they live in `state["messages"]` during
    the agent/tools loop and are removed here at the terminal step.

    Before stripping, every `ToolMessage` is parsed into a renderable
    dict (`{tool, tool_call_id, payload}`) and written to
    `state["tool_results"]`. This is what `ChatService` / the SSE
    stream return to the client — the structured place list the user
    expects alongside the prose. Without this capture step those
    payloads would vanish with the RemoveMessage deletes.

    Emits `RemoveMessage(id=...)` for every `ToolMessage` and every
    `AIMessage` that carried only tool_use blocks (no user-facing
    text). `add_messages_capped` wraps `add_messages`, which natively
    interprets `RemoveMessage` as a delete.

    The agent's final prose `AIMessage` (the one that routed via
    should_continue → "end") survives. Future turns see clean
    human/agent pairs and no tool noise.
    """
    to_remove: list[str] = []
    # Tools write the untrimmed result to `tool_payloads` (ADR-139); the
    # ToolMessage the model read is a lean projection and no longer carries
    # the ids the linker and the signal path need. Parsing the messages is
    # kept only as a fallback for a tool that has not been migrated.
    tool_results: list[dict[str, Any]] = list(state.get("tool_payloads") or [])
    seen_call_ids = {r.get("tool_call_id") for r in tool_results}
    for msg in state.get("messages") or []:
        msg_id = getattr(msg, "id", None)
        if isinstance(msg, ToolMessage):
            if getattr(msg, "tool_call_id", None) not in seen_call_ids:
                tool_results.append(
                    {
                        "tool": getattr(msg, "name", None),
                        "tool_call_id": getattr(msg, "tool_call_id", None),
                        "payload": _parse_tool_message_payload(msg),
                    }
                )
            if msg_id is not None:
                to_remove.append(msg_id)
            continue
        if isinstance(msg, AIMessage) and msg_id is not None:
            tool_calls = getattr(msg, "tool_calls", None) or []
            text = extract_text_content(getattr(msg, "content", None)).strip()
            if tool_calls and not text:
                to_remove.append(msg_id)

    update: dict[str, Any] = {}
    if tool_results:
        update["tool_results"] = tool_results
    if to_remove:
        logger.debug("finalize_node stripping %d tool-related messages", len(to_remove))
        update["messages"] = [RemoveMessage(id=mid) for mid in to_remove]
    return update


def scrub_tool_results_node(state: AgentState) -> dict[str, Any]:  # noqa: ARG001
    """Clear `tool_results` from state so it never lands in the checkpoint.

    `finalize_node` populates `tool_results` so the response layer can
    surface the structured payloads, but those payloads must not bloat
    the per-thread checkpointer DB — only the human-readable
    `reasoning_steps` summaries should persist as agent history. The
    server-side `tool_payloads` channel the tools wrote (ADR-139) is
    cleared here for the same reason.

    This node runs as a separate superstep AFTER `finalize`. Callers
    that need the populated `tool_results` consume `astream(
    stream_mode="values")` and capture the snapshot emitted between
    `finalize` and this node; the final checkpointed state has
    `tool_results=[]`.
    """
    return {"tool_results": [], "tool_payloads": []}


def build_graph(
    llm: Any,
    tools: list[Any],
    checkpointer: Any,
    resolver_llm: Any,
    geocoding_client: Any,
) -> Any:
    """Construct and compile the agent StateGraph (FR-025).

    Nodes: `resolve_location`, `agent`, `tools` (ToolNode), `fallback`,
    `finalize`. The entry point is conditional
    (`_needs_location_resolution` gate): a location-free turn routes
    straight to `agent` and pays no resolver LLM call; otherwise
    `resolve_location` runs first, then `agent`. Conditional edges from
    `agent` via should_continue → {tools, fallback, finalize}. Direct
    edges resolve_location → agent, tools → agent, fallback → finalize,
    finalize → END.

    `finalize` strips ToolMessages and tool-only AIMessages before the
    state hits the checkpointer so tool results never persist into
    future turns — only the agent's final prose AIMessage survives.
    """
    graph: StateGraph = StateGraph(AgentState)
    graph.add_node(
        NODE_RESOLVE_LOCATION,
        make_resolve_location_node(resolver_llm, geocoding_client),
    )
    graph.add_node(NODE_AGENT, make_agent_node(llm, tools))
    graph.add_node(
        NODE_TOOLS, ToolNode(tools, handle_tool_errors=_handle_tool_node_error)
    )
    graph.add_node(NODE_FALLBACK, fallback_node)
    graph.add_node(NODE_FINALIZE, finalize_node)
    graph.add_node(NODE_SCRUB_TOOL_RESULTS, scrub_tool_results_node)
    graph.add_node(NODE_TRIP_GUARD, trip_guard_node)
    graph.set_conditional_entry_point(
        _needs_location_resolution,
        {"resolve": NODE_RESOLVE_LOCATION, "skip": NODE_AGENT},
    )
    graph.add_edge(NODE_RESOLVE_LOCATION, NODE_AGENT)

    # The guard exists to inject a suggest_places call, so it only routes
    # when that tool is actually bound. It is entitlement-gated (discovery
    # tier), and injecting a call for an unbound tool produces a silent
    # error ToolMessage — the failure that hid the guard's whole purpose
    # in live testing (ADR-150).
    has_suggest_places = any(getattr(t, "name", "") == "suggest_places" for t in tools)

    def _route_from_agent(state: AgentState) -> str:
        route = should_continue(state)
        if route == NODE_TRIP_GUARD and not has_suggest_places:
            return "end"
        return route

    graph.add_conditional_edges(
        NODE_AGENT,
        _route_from_agent,
        {
            NODE_TOOLS: NODE_TOOLS,
            NODE_FALLBACK: NODE_FALLBACK,
            NODE_TRIP_GUARD: NODE_TRIP_GUARD,
            "end": NODE_FINALIZE,
        },
    )
    graph.add_edge(NODE_TOOLS, NODE_AGENT)

    # Verification mode appends its own suggest_places call, so it routes
    # through the tool node; the polish mode is text-only and goes straight
    # back to the model for the rewrite.
    def _route_from_guard(state: AgentState) -> str:
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        return NODE_TOOLS if getattr(last, "tool_calls", None) else NODE_AGENT

    graph.add_conditional_edges(
        NODE_TRIP_GUARD,
        _route_from_guard,
        {NODE_TOOLS: NODE_TOOLS, NODE_AGENT: NODE_AGENT},
    )
    graph.add_edge(NODE_FALLBACK, NODE_FINALIZE)
    graph.add_edge(NODE_FINALIZE, NODE_SCRUB_TOOL_RESULTS)
    graph.add_edge(NODE_SCRUB_TOOL_RESULTS, END)
    return graph.compile(checkpointer=checkpointer)
