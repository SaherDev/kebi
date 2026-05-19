"""Tests for LLMResolver — pre-search resolve pass (ADR-080)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.core.extraction.candidate_mapper import normalize_query
from kebi.core.extraction.enrichers.llm_resolver import (
    LLMResolver,
    _ResolvedCandidate,
    _ResolverLocation,
    _ResolverResponse,
    _ResolverTag,
)
from kebi.core.extraction.types import (
    ExtractionContext,
    KnownPlace,
    Medium,
    Producer,
)
from kebi.core.places import TagType


def _ctx(names: list[str], **kw: object) -> ExtractionContext:
    ctx = ExtractionContext(url="https://x.com", user_id="u1")
    for n in names:
        ctx.known_places.append(
            KnownPlace(name=n, producer=Producer.VISION_IMAGES, medium=Medium.IMAGE)
        )
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


def _resolver(response: _ResolverResponse | Exception) -> LLMResolver:
    instructor = MagicMock()
    if isinstance(response, Exception):
        instructor.extract = AsyncMock(side_effect=response)
    else:
        instructor.extract = AsyncMock(return_value=response)
    return LLMResolver(instructor_client=instructor)


@pytest.mark.asyncio
async def test_no_known_places_skips_llm_and_returns_location_hint() -> None:
    instructor = MagicMock()
    instructor.extract = AsyncMock()
    resolver = LLMResolver(instructor_client=instructor)

    ctx = ExtractionContext(url="https://x.com", user_id="u1")
    ctx.location_tag = "Bangkok"
    out = await resolver.resolve(ctx)

    instructor.extract.assert_not_awaited()
    assert out.queries == {}
    assert out.location is not None and out.location.address == "Bangkok"
    assert out.post_tags == []


@pytest.mark.asyncio
async def test_cleans_queries_keyed_by_normalized_raw_name() -> None:
    resp = _ResolverResponse(
        candidates=[
            _ResolvedCandidate(
                raw_name="1. Restaurant POTONG",
                search_query="Restaurant POTONG",
            ),
            _ResolvedCandidate(raw_name="SORN", search_query="Sorn Bangkok"),
        ],
        location=_ResolverLocation(city="Bangkok", country="Thailand"),
        post_tags=[_ResolverTag(type="atmosphere", value="upscale")],
    )
    out = await _resolver(resp).resolve(_ctx(["1. Restaurant POTONG", "SORN"]))

    assert out.queries == {
        normalize_query("1. Restaurant POTONG"): "Restaurant POTONG",
        normalize_query("SORN"): "Sorn Bangkok",
    }
    assert out.location is not None
    assert out.location.city == "Bangkok"
    assert out.location.country == "Thailand"
    assert len(out.post_tags) == 1
    assert out.post_tags[0].type == TagType.atmosphere
    assert out.post_tags[0].value == "upscale"
    assert out.post_tags[0].source == "llm"


@pytest.mark.asyncio
async def test_dropped_noise_absent_from_queries() -> None:
    # Resolver omits "Top 5 Restaurants" (a header) entirely.
    resp = _ResolverResponse(
        candidates=[
            _ResolvedCandidate(raw_name="Mezzaluna", search_query="Mezzaluna Bangkok"),
        ],
        location=_ResolverLocation(),
        post_tags=[],
    )
    out = await _resolver(resp).resolve(_ctx(["Top 5 Restaurants", "Mezzaluna"]))

    assert normalize_query("Mezzaluna") in out.queries
    assert normalize_query("Top 5 Restaurants") not in out.queries


@pytest.mark.asyncio
async def test_llm_failure_degrades_to_identity_map() -> None:
    resolver = _resolver(RuntimeError("boom"))
    ctx = _ctx(["Sorn", "Mezzaluna"], location_tag="Bangkok")

    out = await resolver.resolve(ctx)

    assert out.queries == {
        normalize_query("Sorn"): "Sorn",
        normalize_query("Mezzaluna"): "Mezzaluna",
    }
    # Degraded display labels mirror the identity map (raw names).
    assert out.display_labels == {
        normalize_query("Sorn"): "Sorn",
        normalize_query("Mezzaluna"): "Mezzaluna",
    }
    assert out.location is not None and out.location.address == "Bangkok"
    assert out.post_tags == []


@pytest.mark.asyncio
async def test_display_label_distinct_from_search_query() -> None:
    """ADR-081: display_label is the clean name the user saw, NOT the
    swapped-in real name (search_query) and NOT the raw numbered OCR."""
    resp = _ResolverResponse(
        candidates=[
            _ResolvedCandidate(
                raw_name="1. Mirror Temple",
                search_query="Wat Phuttha Prommayan",
                display_label="Mirror Temple",
            ),
            # Model left display_label blank → fall back to raw name.
            _ResolvedCandidate(raw_name="SORN", search_query="Sorn Bangkok"),
        ],
        location=_ResolverLocation(),
        post_tags=[],
    )
    out = await _resolver(resp).resolve(_ctx(["1. Mirror Temple", "SORN"]))

    assert out.display_labels == {
        normalize_query("1. Mirror Temple"): "Mirror Temple",
        normalize_query("SORN"): "SORN",
    }
    # search_query is unchanged by this (still the real/searchable name).
    assert out.queries[normalize_query("1. Mirror Temple")] == (
        "Wat Phuttha Prommayan"
    )
