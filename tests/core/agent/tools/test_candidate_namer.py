"""Tests for `CandidateNamerService`.

Exercises the service against a stubbed Instructor client so we can
assert (a) the prompt is rendered with every required slot populated and
(b) failures degrade silently to an empty list of candidates without
raising — the consuming tool maps that to `empty_reason="no_match"`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.core.agent.location import WorkingLocation
from kebi.core.agent.tools.candidate_namer import (
    CandidateName,
    CandidateNamerService,
    CandidateNames,
)
from kebi.core.places.models import PlaceCategory


def _bangkok() -> WorkingLocation:
    return WorkingLocation(
        country="Thailand",
        city="Bangkok",
        neighborhood="Sukhumvit",
        lat=13.7563,
        lng=100.5018,
        density="dense",
        effective_mode="walking",
        scope_tier="walkable",
        scope_shape="area",
        search_radius_m=1200.0,
    )


def _stub_client(
    *,
    response: CandidateNames | None = None,
    raises: Exception | None = None,
) -> MagicMock:
    """Build a stand-in InstructorClient with a controllable `extract` method."""
    client = MagicMock()
    if raises is not None:
        client.extract = AsyncMock(side_effect=raises)
    else:
        client.extract = AsyncMock(
            return_value=response or CandidateNames(candidates=[])
        )
    return client


@pytest.mark.asyncio
async def test_prompt_slots_populated() -> None:
    """Every required template slot is rendered with usable content."""
    client = _stub_client(
        response=CandidateNames(
            candidates=[
                CandidateName(name="Gaa", reason="acclaimed plant-forward menu"),
            ]
        )
    )
    namer = CandidateNamerService(instructor_client=client)

    result = await namer.generate(
        intent="famous vegetarian-friendly dinner",
        working=_bangkok(),
        categories=[PlaceCategory.restaurant],
        tags=["vegetarian"],
        taste_summary="loves bold flavors, prefers tasting menus",
        count=8,
        user_id="user-1",
    )

    assert len(result.candidates) == 1
    assert result.candidates[0].name == "Gaa"
    assert result.candidates[0].reason

    # Inspect the prompt actually handed to Instructor.
    call_args = client.extract.await_args
    messages = call_args.kwargs["messages"]
    assert len(messages) == 1
    rendered = messages[0]["content"]
    # Location anchoring + radius are load-bearing — must be in the prompt.
    assert "Bangkok" in rendered
    assert "Thailand" in rendered
    assert "Sukhumvit" in rendered
    assert "1200 metres" in rendered
    # Mobility, intent, constraints, categories, taste, count all rendered.
    assert "Effective mode: walking" in rendered
    assert "famous vegetarian-friendly dinner" in rendered
    assert "vegetarian" in rendered
    assert "restaurant" in rendered
    assert "loves bold flavors" in rendered
    assert "up to 8" in rendered


@pytest.mark.asyncio
async def test_unconstrained_fields_render_as_placeholders() -> None:
    """Missing categories / tags / taste yield 'none' placeholders, not blanks."""
    client = _stub_client(response=CandidateNames(candidates=[]))
    namer = CandidateNamerService(instructor_client=client)

    await namer.generate(
        intent="anything good",
        working=_bangkok(),
        categories=None,
        tags=None,
        taste_summary="",
        count=5,
    )
    rendered = client.extract.await_args.kwargs["messages"][0]["content"]
    assert "(none — categories unconstrained)" in rendered
    assert "(none — hard constraints unconstrained)" in rendered
    assert "(no prior taste signal" in rendered


@pytest.mark.asyncio
async def test_exception_degrades_to_empty_candidates() -> None:
    """A raising Instructor call must not propagate — return empty result."""
    client = _stub_client(raises=RuntimeError("simulated instructor failure"))
    namer = CandidateNamerService(instructor_client=client)

    result = await namer.generate(
        intent="ramen",
        working=_bangkok(),
        categories=None,
        tags=None,
        taste_summary=None,
        count=8,
    )
    assert result.candidates == []


@pytest.mark.asyncio
async def test_response_passed_through_verbatim() -> None:
    """The service does not filter / reshape candidates — that's the tool's job."""
    canned = CandidateNames(
        candidates=[
            CandidateName(name="Place A", reason="reason A"),
            CandidateName(name="Place B", reason="reason B"),
            CandidateName(name="Place C", reason="reason C"),
        ]
    )
    namer = CandidateNamerService(instructor_client=_stub_client(response=canned))
    result = await namer.generate(
        intent="anything",
        working=_bangkok(),
        categories=None,
        tags=None,
        taste_summary=None,
        count=8,
    )
    assert [(c.name, c.reason) for c in result.candidates] == [
        ("Place A", "reason A"),
        ("Place B", "reason B"),
        ("Place C", "reason C"),
    ]


@pytest.mark.asyncio
async def test_tracer_receives_anchor_metadata(monkeypatch) -> None:
    """Tracing span gets the location + radius — useful for Langfuse debugging."""
    captured: dict[str, Any] = {}

    class _FakeTracer:
        def generation(self, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            from kebi.providers.tracing import _NullSpan

            return _NullSpan()

    fake = _FakeTracer()
    monkeypatch.setattr(
        "kebi.core.agent._trace_context.get_tracing_client", lambda: fake
    )

    client = _stub_client(response=CandidateNames(candidates=[]))
    namer = CandidateNamerService(instructor_client=client)

    await namer.generate(
        intent="ramen",
        working=_bangkok(),
        categories=None,
        tags=None,
        taste_summary=None,
        count=8,
        user_id="u-99",
    )
    assert captured["name"] == "candidate_namer"
    assert captured["user_id"] == "u-99"
    assert captured["input"]["city"] == "Bangkok"
    assert captured["input"]["search_radius_m"] == 1200
    assert captured["input"]["effective_mode"] == "walking"
