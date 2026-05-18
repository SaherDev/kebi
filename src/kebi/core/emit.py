"""EmitFn — primitive callback Protocol for pipeline-stage emission (feature 028 M4).

The extraction pipeline accepts an optional `emit: EmitFn | None = None`
parameter and calls `emit(step, summary)` — or
`emit(step, summary, duration_ms=elapsed)` when it measured the
operation directly — at each pipeline boundary.

Services never construct `ReasoningStep` objects and never import from
`core/agent/*`. (ADR-075 removed the recall/consult tools and their
emit-closure wrappers; this Protocol remains for the extraction
pipeline's progress emission.)

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
