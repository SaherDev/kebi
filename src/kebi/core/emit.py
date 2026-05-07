"""EmitFn — primitive callback Protocol for pipeline-stage emission (feature 028 M4).

Services (`RecallService`, `ConsultService`, `ExtractionService`) accept an
optional `emit: EmitFn | None = None` parameter and call
`emit(step, summary)` — or `emit(step, summary, duration_ms=elapsed)` when
the service measured the operation directly — at each pipeline boundary.

Services never construct `ReasoningStep` objects and never import from
`core/agent/*`. Agent-layer fields (`source`, `tool_name`, `visibility`,
`timestamp`, `duration_ms`) are stamped by the tool wrapper's emit closure
in `core/agent/tools/_emit.py`.

`EmitFn` must be a `typing.Protocol` (not a plain `Callable` alias),
because the third positional argument `duration_ms` has a default value —
`Callable[[str, str, float | None], None]` cannot express that.
"""

from __future__ import annotations

from typing import Protocol


class EmitFn(Protocol):
    def __call__(
        self,
        step: str,
        summary: str,
        duration_ms: float | None = None,
    ) -> None: ...


__all__ = ["EmitFn"]
