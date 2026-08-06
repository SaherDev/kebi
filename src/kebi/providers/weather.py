"""Current-conditions provider — Protocol plus a null implementation (ADR-144).

Season is derivable from the calendar; weather is not. And in the tropics the
calendar says nothing useful at all — there is no meaningful summer or winter
in Bali, but there is very much a difference between a wet afternoon and a dry
one, and it decides whether the answer is a beach club or a room with a roof.

No weather source is wired yet. What exists here is the seam: a Protocol and a
null adapter, so adding a real source later is one class and one dependency
line rather than a change threaded through the agent. That follows the same
rule as every other external dependency in this repo — the abstraction lands
on day one, not after the third caller has imported an SDK directly.

The null adapter returns `None`, which every consumer already handles as
"unknown", so today's behaviour is unchanged: the calendar season is used
where it means something, and nothing is asserted where it does not.
"""

from __future__ import annotations

from typing import Protocol


class WeatherProvider(Protocol):
    """Current conditions at a point, in controlled-vocabulary terms."""

    async def current(self, *, lat: float, lng: float) -> str | None:
        """A `SeasonTag` value describing conditions now, or None if unknown.

        Returning a vocabulary value rather than a temperature or a provider's
        own code is deliberate: claims are tagged from that vocabulary, so the
        ranker can match conditions against them directly. `rainy` is the one
        that earns its keep; `summer` and `winter` mostly restate the
        calendar.

        Implementations must never raise. Weather is decoration on an answer,
        not a precondition for one — a provider outage should cost a nuance,
        never a turn.
        """
        ...


class NullWeatherProvider:
    """No weather source configured; conditions are simply unknown."""

    async def current(self, *, lat: float, lng: float) -> str | None:  # noqa: ARG002
        return None
