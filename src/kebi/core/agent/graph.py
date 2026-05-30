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
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from kebi.core.agent._trace_context import feature_span
from kebi.core.agent.location import (
    CorridorTarget,
    LocationResolution,
    WorkingLocation,
    density_class,
    resolve_radius,
)
from kebi.core.agent.messages import extract_text_content
from kebi.core.agent.reasoning import ReasoningStep
from kebi.core.agent.state import AgentState
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
# returns empty content. The agent has no tools (ADR-075), so a direct
# response is the only outcome.
_DIRECT_RESPONSE_FALLBACK = "responding directly"

# Node names are re-used by tests asserting graph structure.
NODE_RESOLVE_LOCATION = "resolve_location"
NODE_AGENT = "agent"
NODE_TOOLS = "tools"
NODE_FALLBACK = "fallback"
NODE_FINALIZE = "finalize"
NODE_SCRUB_TOOL_RESULTS = "scrub_tool_results"

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
            value = r.get("place_name")
            if isinstance(value, str):
                nm = value
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

    The reasoning step's `summary` carries `AIMessage.content` truncated
    to 200 chars. When `content` is empty (tool-call-only response), a
    synthesized fallback keyed by the first tool-call name is used. A
    streaming caller (via `get_stream_writer()`) receives the full,
    untruncated text.
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
                summary=f"Connection error ({type(exc).__name__}) — please retry",
                source="agent",
                visibility="user",
                duration_ms=0.0,
            )
            return {
                "messages": [error_msg],
                "error_count": state.get("error_count", 0) + 1,
                "steps_taken": state.get("steps_taken", 0) + 1,
                "reasoning_steps": (state.get("reasoning_steps") or []) + [step],
            }

        full_text = extract_text_content(getattr(ai_msg, "content", None)).strip()

        summary_source = full_text if full_text else _DIRECT_RESPONSE_FALLBACK

        try:
            writer = get_stream_writer()
        except RuntimeError:
            writer = None
        if writer is not None:
            writer({"step": "agent.tool_decision", "summary": summary_source})

        step = ReasoningStep(
            step="agent.tool_decision",
            summary=summary_source[:200],
            source="agent",
            visibility="user",
            duration_ms=0.0,
        )
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
      otherwise                          → "end"

    The tool-call cap is checked first so the cap-hit case owns its
    branch in `fallback_node` rather than degrading to the generic
    "something went wrong" message that `max_errors` / `max_steps` use.
    """
    cfg = get_config().agent
    if state.get("tool_calls_used", 0) >= cfg.max_tool_calls:
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
    return "end"


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
    return "skip"


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
                return WorkingLocation(**prior_working_location)
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
            lat=lat,
            lng=lng,
            density=density_class(rev.place_type),
            bbox=rev.bbox,
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
        lat=geo.lat,
        lng=geo.lng,
        density=density_class(geo.place_type),
        bbox=geo.bbox,
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
            "scope_shape": resolution.scope_shape,
            "search_radius_m": radius,
            "corridor": corridor,
        }
    )


def _emit_step(step: ReasoningStep) -> None:
    """Fan a reasoning step out to a streaming caller, if any."""
    try:
        writer = get_stream_writer()
    except RuntimeError:
        writer = None
    if writer is not None:
        writer({"step": step.step, "summary": step.summary})


def _location_clarification_update(state: AgentState, reason: str) -> dict[str, Any]:
    """State update that clears the working location and asks the user."""
    step = ReasoningStep(
        step="agent.location_clarify",
        summary=f"Location needs clarification: {reason}",
        source="agent",
        visibility="user",
        duration_ms=0.0,
    )
    _emit_step(step)
    return {
        "working_location": None,
        "location_clarification": reason,
        "reasoning_steps": (state.get("reasoning_steps") or []) + [step],
    }


def _location_resolved_update(
    state: AgentState, working: WorkingLocation
) -> dict[str, Any]:
    """State update that records a fully resolved working location."""
    parts = [working.neighborhood, working.city, working.country]
    place = ", ".join(p for p in parts if p)
    step = ReasoningStep(
        step="agent.location_resolved",
        summary=f"Working location: {place}",
        source="agent",
        visibility="user",
        duration_ms=0.0,
    )
    _emit_step(step)
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
            return _location_clarification_update(state, _LOCATION_ASK_CITY)

        # Defensive: include_raw=True can return a dict with parsing_error
        # set when the model output failed schema validation. Treat that
        # the same as an LLM failure → ask the user.
        if isinstance(result, dict):
            parse_err = result.get("parsing_error")
            parsed = result.get("parsed")
            if parse_err is not None or parsed is None:
                logger.warning("resolve_location parsing_error: %s", parse_err)
                return _location_clarification_update(state, _LOCATION_ASK_CITY)
            resolution: LocationResolution = parsed
        else:
            # Older / cached `with_structured_output` without include_raw —
            # treat the result as the parsed model directly.
            resolution = result

        if resolution.needs_clarification or resolution.is_ambiguous:
            reason = (
                resolution.clarification_reason.strip()
                or "the location you mean is ambiguous"
            )
            return _location_clarification_update(state, reason)

        try:
            working = await _build_working_location(
                resolution,
                state.get("user_location"),
                _carried_working_location(state),
                geocoding_client,
            )
            if working is None:
                return _location_clarification_update(state, _LOCATION_ASK_CONFIRM)
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
            return _location_clarification_update(state, _LOCATION_ASK_CONFIRM)

        if working is None:
            # The point resolved, but a corridor destination did not — ask
            # rather than silently degrading to an area search.
            return _location_clarification_update(state, _CORRIDOR_ASK)
        return _location_resolved_update(state, working)

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
            f"Couldn't get there in {cfg.max_tool_calls} tool calls — "
            "the query needs more detail to narrow things down"
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
        summary = (
            f"Got stuck after {cfg.max_steps} steps, something went wrong on my end"
        )
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
        summary = "Hit too many errors, try rephrasing or sharing more detail"
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
        summary = "Something went wrong on my end"

    tracer = get_tracing_client()
    span = tracer.generation("agent_fallback", user_id=state.get("user_id"))
    span.end(output={"error_type": error_type}, level="ERROR")

    user_step = ReasoningStep(
        step="fallback",
        summary=summary,
        source="fallback",
        visibility="user",
    )
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
    tool_results: list[dict[str, Any]] = []
    for msg in state.get("messages") or []:
        msg_id = getattr(msg, "id", None)
        if isinstance(msg, ToolMessage):
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
    `reasoning_steps` summaries should persist as agent history.

    This node runs as a separate superstep AFTER `finalize`. Callers
    that need the populated `tool_results` consume `astream(
    stream_mode="values")` and capture the snapshot emitted between
    `finalize` and this node; the final checkpointed state has
    `tool_results=[]`.
    """
    return {"tool_results": []}


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
    graph.set_conditional_entry_point(
        _needs_location_resolution,
        {"resolve": NODE_RESOLVE_LOCATION, "skip": NODE_AGENT},
    )
    graph.add_edge(NODE_RESOLVE_LOCATION, NODE_AGENT)
    graph.add_conditional_edges(
        NODE_AGENT,
        should_continue,
        {
            NODE_TOOLS: NODE_TOOLS,
            NODE_FALLBACK: NODE_FALLBACK,
            "end": NODE_FINALIZE,
        },
    )
    graph.add_edge(NODE_TOOLS, NODE_AGENT)
    graph.add_edge(NODE_FALLBACK, NODE_FINALIZE)
    graph.add_edge(NODE_FINALIZE, NODE_SCRUB_TOOL_RESULTS)
    graph.add_edge(NODE_SCRUB_TOOL_RESULTS, END)
    return graph.compile(checkpointer=checkpointer)
