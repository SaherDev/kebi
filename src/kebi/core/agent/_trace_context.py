"""Per-tool tracing context + small helpers for agent-attributable spans.

Tools set `current_tool` on entry so nested paid services
(VoyageEmbedder, CandidateNamerService, and — once subtask 3 lands —
GooglePlacesClient) inherit the tool name without threading args
through every signature. Langfuse's own SDK contextvars handle span
nesting under a parent trace; this ContextVar carries only Kebi's
feature/tool tag.

Concurrency: a tool that spawns subtasks via `asyncio.create_task`
after `set_tool(...)` will give those subtasks a snapshot of the
parent's tool name at task-creation time (standard ContextVar
semantics — copy-on-create). A subtask that calls `set_tool(...)`
itself does not propagate the change back to the parent. No agent
tool currently uses `create_task` after `set_tool`; a regression test
in `tests/core/agent/test_trace_context.py` guards against future
drift.

`feature_trace(...)` and `feature_span(...)` are one-line wrappers
that bundle the trace/generation setup every paid call site repeats.
The verbose form is `get_tracing_client().trace(name=..., user_id=...,
metadata={"feature": ...}, tags=["feature:..."])`; the helper collapses
that to `async with feature_trace("agent", user_id): ...`.

Instructor's internal `max_retries` is opaque to us — when Instructor
retries inside one external `extract_with_completion(...)` call, only
the final completion's usage is surfaced. Multiple internal retries
collapse into one span. Revisit in subtask 4 if reconciliation drifts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any

from kebi.core.config import get_config
from kebi.providers.tracing import TracingSpan, get_tracing_client


def _model_for_role(role: str | None) -> str | None:
    """Resolve the configured model name for a logical role, or None.

    Centralises the `get_config().models[role].model` lookup so services
    pass a role they already know (taste_regen, memory_extractor, …) and
    never read the model name off the LLM client. A missing role degrades
    silently to `None`; Langfuse records the span without pricing rather
    than failing the call.
    """
    if role is None:
        return None
    try:
        return get_config().models[role].model
    except (KeyError, AttributeError):
        return None


current_tool: ContextVar[str | None] = ContextVar("current_tool", default=None)


@contextmanager
def set_tool(name: str) -> Iterator[None]:
    """Set `current_tool` for the duration of the block, reset on exit."""
    token = current_tool.set(name)
    try:
        yield
    finally:
        current_tool.reset(token)


def _build_meta(
    feature: str,
    tool: str | None,
    extra: dict[str, Any] | None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {"feature": feature}
    if tool is not None:
        meta["tool"] = tool
    if extra:
        meta.update(extra)
    return meta


@asynccontextmanager
async def feature_trace(
    feature: str,
    user_id: str | None,
    *,
    name: str | None = None,
    extra: dict[str, Any] | None = None,
) -> AsyncIterator[None]:
    """Open a Langfuse trace tagged with the feature + user.

    Tags belong on the trace (Langfuse v3 has no observation-level
    tags); generations created inside this block inherit the trace via
    the SDK's contextvar. `name` defaults to the feature so 'feature'
    and 'name' don't go out of sync at every call site.
    """
    tracer = get_tracing_client()
    tags = [f"feature:{feature}"]
    async with tracer.trace(
        name=name or feature,
        user_id=user_id,
        session_id=user_id,
        metadata=_build_meta(feature, None, extra),
        tags=tags,
    ):
        yield


def feature_span(
    name: str,
    feature: str,
    *,
    user_id: str | None = None,
    role: str | None = None,
    model: str | None = None,
    tool: str | None = None,
    extra: dict[str, Any] | None = None,
    input: Any = None,
) -> TracingSpan:
    """Low-level primitive: open a Langfuse generation. Most services should
    use `traced_call(...)` instead — it auto-ends the span on exit.

    `role` is the logical role from `config/app.yaml` (taste_regen,
    memory_extractor, orchestrator, …). Pass `role`, not `model`;
    the helper resolves the model name from config so service code
    never reads `llm.model`. `model` is kept as a low-level escape
    hatch for cases where the call site has a model string but no role
    (e.g. embeddings — `VoyageEmbedder` knows its model directly).
    Reads `current_tool` when `tool` is not passed explicitly.
    """
    if tool is None:
        tool = current_tool.get()
    return get_tracing_client().generation(
        name=name,
        input=input,
        model=model or _model_for_role(role),
        user_id=user_id,
        metadata=_build_meta(feature, tool, extra),
    )


class TracedCall:
    """Handle yielded by `traced_call(...)` — service mutates fields, the
    context manager ends the span with those values on exit.

    The service makes one call (`async with traced_call(...) as t:`) and
    forgets about span lifecycle. To record usage / output set `t.usage`
    / `t.output` inside the block; to mark a swallowed error use
    `t.fail(exc)`. An exception that escapes the block is auto-marked
    ERROR and re-raised.

    `cost_usd` is for providers Langfuse's catalog doesn't price
    (Voyage, Whisper, Google Places, Apify). When set, it's sent as
    `cost_details={"total": cost_usd}` on span end. Leave None for
    token-priced LLMs — Langfuse will price those from model + usage.
    """

    __slots__ = ("span", "output", "usage", "level", "cost_usd")

    def __init__(self, span: TracingSpan) -> None:
        self.span = span
        self.output: dict[str, Any] | None = None
        self.usage: dict[str, int] | None = None
        self.level: str = "DEFAULT"
        self.cost_usd: float | None = None

    def fail(self, exc: object) -> None:
        self.level = "ERROR"
        self.output = {"error": str(exc)}


@asynccontextmanager
async def traced_call(
    name: str,
    feature: str,
    *,
    user_id: str | None = None,
    role: str | None = None,
    model: str | None = None,
    tool: str | None = None,
    extra: dict[str, Any] | None = None,
    input: Any = None,
    standalone: bool = False,
) -> AsyncIterator[TracedCall]:
    """Wrap one LLM call with a Langfuse span.

    Service code makes ONE call:

        async with traced_call("taste_regen.llm", "taste_regen",
                               role="taste_regen", user_id=user_id,
                               standalone=True) as t:
            raw = await llm.complete(messages)
            t.output = {"text": raw}

    The span is opened on entry, ended on exit. An escaping exception
    marks the span ERROR and is re-raised. Inside the block, set
    `t.output` / `t.usage`, or call `t.fail(exc)` for a swallowed error.

    `role` is the logical role from `config/app.yaml`; the helper
    resolves the model from it. Pass `model=` only when the call site
    has no role (rare — embeddings).

    `standalone=True` adds an enclosing `feature_trace` so the span is
    its own root trace (background handlers — memory extractor, taste
    regen — where no chat-turn trace is active).
    """
    if standalone:
        async with (
            feature_trace(feature, user_id),
            _traced_span(
                name, feature, user_id, role, model, tool, extra, input
            ) as call,
        ):
            yield call
        return
    async with _traced_span(
        name, feature, user_id, role, model, tool, extra, input
    ) as call:
        yield call


@asynccontextmanager
async def _traced_span(
    name: str,
    feature: str,
    user_id: str | None,
    role: str | None,
    model: str | None,
    tool: str | None,
    extra: dict[str, Any] | None,
    input: Any,
) -> AsyncIterator[TracedCall]:
    span = feature_span(
        name,
        feature,
        user_id=user_id,
        role=role,
        model=model,
        tool=tool,
        extra=extra,
        input=input,
    )
    call = TracedCall(span)
    try:
        yield call
    except Exception as exc:
        if call.level == "DEFAULT":
            call.fail(exc)
        _end_span(call)
        raise
    _end_span(call)


def _end_span(call: TracedCall) -> None:
    """End the span with whatever fields the caller mutated."""
    cost = {"total": call.cost_usd} if call.cost_usd is not None else None
    call.span.end(
        level=call.level,
        output=call.output,
        usage=call.usage,
        cost_details=cost,
    )
