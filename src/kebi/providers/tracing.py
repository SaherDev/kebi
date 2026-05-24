"""Tracing provider abstraction (ADR-025).

Callers depend on TracingClient / TracingSpan protocols.
The Langfuse adapter is the default implementation; swap by returning a
different adapter from get_tracing_client().

Extension point (Phase 4.5, agent cost visibility): the Protocol carries
`tags`, `metadata`, and `usage` so child spans can be attributed to a
feature, a tool, and a user. The `trace()` API opens a root observation
as an async context manager — every `generation(...)` call made inside
the block nests under that trace automatically (Langfuse's own SDK uses
contextvars). Subtasks 2 (extraction) and 3 (external APIs) consume this
shape without further infrastructure work.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class TracingSpan(Protocol):
    def end(
        self,
        output: dict[str, Any] | None = None,
        level: str = "DEFAULT",
        usage: dict[str, int] | None = None,
        metadata: dict[str, Any] | None = None,
        cost_details: dict[str, float] | None = None,
    ) -> None: ...


@runtime_checkable
class TracingClient(Protocol):
    def generation(
        self,
        name: str,
        input: Any = None,
        model: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> TracingSpan: ...

    def trace(
        self,
        name: str,
        user_id: str | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> Any: ...
    # `trace` returns an async context manager. Typing it as
    # `AbstractAsyncContextManager[None]` would be more precise, but
    # Protocol implementations diverge on return-type narrowing, so `Any`
    # keeps both adapters source-compatible. Callers always use it as
    # `async with tracer.trace(...): ...`.

    def capture_message(
        self,
        message: str,
        level: str = "info",
        metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> None: ...

    def flush(self) -> None: ...


# ---------------------------------------------------------------------------
# Null adapter (no-op — used when Langfuse is not configured)
# ---------------------------------------------------------------------------


class _NullSpan:
    def end(
        self,
        output: dict[str, Any] | None = None,
        level: str = "DEFAULT",
        usage: dict[str, int] | None = None,
        metadata: dict[str, Any] | None = None,
        cost_details: dict[str, float] | None = None,
    ) -> None:
        pass


class _NullTracingClient:
    def generation(
        self,
        name: str,
        input: Any = None,
        model: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> _NullSpan:
        return _NullSpan()

    @asynccontextmanager
    async def trace(
        self,
        name: str,
        user_id: str | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> AsyncIterator[None]:
        yield

    def capture_message(
        self,
        message: str,
        level: str = "info",
        metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        pass

    def flush(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Langfuse adapter
# ---------------------------------------------------------------------------


class _LangfuseSpan:
    """Handle returned by `generation()`.

    Wraps Langfuse's `start_as_current_observation` context manager so
    the observation is registered as the active parent in Langfuse's
    OTel contextvars between creation and `.end()`. Any nested
    `generation(...)` call made in between inherits this span as its
    parent automatically — that's what makes per-turn cost aggregation
    work (every LLM call under one chat_turn lands as a child).

    Callers MUST end spans in LIFO order (the convention every site in
    this repo follows: open span → make call → end span, single-shot).
    Out-of-order ends would corrupt the OTel contextvar stack.
    """

    def __init__(self, observation: Any, ctx_manager: Any) -> None:
        self._observation = observation
        self._ctx_manager = ctx_manager

    def end(
        self,
        output: dict[str, Any] | None = None,
        level: str = "DEFAULT",
        usage: dict[str, int] | None = None,
        metadata: dict[str, Any] | None = None,
        cost_details: dict[str, float] | None = None,
    ) -> None:
        update_kwargs: dict[str, Any] = {"level": level}
        if output is not None:
            update_kwargs["output"] = output
        if usage is not None:
            # Langfuse v3 expects `usage_details` with input/output/total
            # integer token counts. The model on the observation is what
            # Langfuse uses to price the call from its catalog.
            update_kwargs["usage_details"] = usage
        if metadata is not None:
            update_kwargs["metadata"] = metadata
        if cost_details is not None:
            # Caller-supplied USD cost for providers Langfuse's catalog
            # doesn't price (Voyage, Whisper, Google Places, Apify).
            # Overrides server-side pricing when both are set.
            update_kwargs["cost_details"] = cost_details
        self._observation.update(**update_kwargs)
        # Exiting the context manager auto-ends the observation AND pops
        # it from the OTel contextvar stack, restoring the prior parent.
        self._ctx_manager.__exit__(None, None, None)


class _LangfuseTracingClient:
    def __init__(self, client: Any) -> None:
        self._client = client

    def generation(
        self,
        name: str,
        input: Any = None,
        model: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> _LangfuseSpan:
        """Create a Langfuse generation observation nested under the
        currently-active parent (the chat_turn / feature_trace, when one
        is open via `trace()`).

        Uses `start_as_current_observation` rather than `start_observation`
        because in Langfuse v3, only the `_as_current_` variant registers
        the observation in the OTel contextvar stack — children created
        afterwards inherit it. Without this, every generation lands as
        its own root trace and per-turn cost aggregation breaks.

        Tags are trace-level in Langfuse v3, not observation-level — they
        belong on the enclosing `trace(...)`. The `tags` arg is accepted
        for Protocol parity and ignored here.
        """
        kwargs: dict[str, Any] = {"name": name}
        if input is not None:
            kwargs["input"] = input
        if model is not None:
            kwargs["model"] = model
        meta: dict[str, Any] = dict(metadata or {})
        if user_id is not None:
            meta["user_id"] = user_id
        if session_id is not None:
            meta["session_id"] = session_id
        if meta:
            kwargs["metadata"] = meta
        del tags  # observation-level tags don't exist in v3; see docstring
        cm = self._client.start_as_current_observation(
            as_type="generation", **kwargs
        )
        observation = cm.__enter__()
        return _LangfuseSpan(observation, cm)

    @asynccontextmanager
    async def trace(
        self,
        name: str,
        user_id: str | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> AsyncIterator[None]:
        """Open a root observation that nests every child span beneath it.

        Implemented as an async context manager over Langfuse v3's
        synchronous `start_as_current_observation` — entering/exiting the
        context manager only pushes/pops a contextvar, so wrapping it in
        an async `yield` is sound. Inside the `async with` block, every
        `start_observation(...)` call (including ones in legacy code
        paths that haven't migrated to this API) automatically inherits
        the parent trace via the SDK's contextvars.
        """
        with self._client.start_as_current_observation(as_type="span", name=name):
            update_kwargs: dict[str, Any] = {}
            if user_id is not None:
                update_kwargs["user_id"] = user_id
            if session_id is not None:
                update_kwargs["session_id"] = session_id
            if metadata:
                update_kwargs["metadata"] = metadata
            if tags:
                update_kwargs["tags"] = tags
            if update_kwargs:
                try:
                    self._client.update_current_trace(**update_kwargs)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("update_current_trace failed: %s", exc)
            yield

    def capture_message(
        self,
        message: str,
        level: str = "info",
        metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Record a one-shot event under the current trace.

        Same nesting rule as `generation()` — uses the `_as_current_`
        variant so the event lands under the active chat_turn or
        feature_trace. When the event creates its own root trace (the
        dispatcher fires after chat_turn has closed via FastAPI
        BackgroundTasks), `user_id` / `session_id` are stamped on the
        trace so it remains user-filterable in Langfuse.
        """
        meta: dict[str, Any] = {"level": level, **(metadata or {})}
        if user_id is not None:
            meta["user_id"] = user_id
        if session_id is not None:
            meta["session_id"] = session_id
        with self._client.start_as_current_observation(
            as_type="event", name=message, input=meta
        ):
            update_kwargs: dict[str, Any] = {}
            if user_id is not None:
                update_kwargs["user_id"] = user_id
            if session_id is not None:
                update_kwargs["session_id"] = session_id
            if update_kwargs:
                try:
                    self._client.update_current_trace(**update_kwargs)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "update_current_trace failed on event: %s", exc
                    )

    def flush(self) -> None:
        self._client.flush()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_UNSET: object = object()
_client: _LangfuseTracingClient | _NullTracingClient | object = _UNSET


def get_tracing_client() -> TracingClient:
    """Return a TracingClient. Always returns a valid client — never None.

    Result is cached after first call. Falls back to a no-op client when
    Langfuse SDK is missing or credentials are absent.
    """
    global _client
    if _client is not _UNSET:
        return _client  # type: ignore[return-value]

    try:
        import langfuse  # noqa: PLC0415

        from kebi.core.config import get_env  # noqa: PLC0415

        secrets = get_env()
        lf = langfuse.Langfuse(
            public_key=secrets.LANGFUSE_PUBLIC_KEY,
            secret_key=secrets.LANGFUSE_SECRET_KEY,
            host=secrets.LANGFUSE_HOST,
        )
        lf.auth_check()
        _client = _LangfuseTracingClient(lf)
    except Exception as exc:
        logger.warning("Langfuse tracing disabled: %s", exc)
        _client = _NullTracingClient()

    assert isinstance(_client, _LangfuseTracingClient | _NullTracingClient)
    return _client
