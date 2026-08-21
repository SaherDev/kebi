"""Tests for LLMResolver — pre-search resolve pass (ADR-080)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.core.extraction.candidate_mapper import normalize_query
from kebi.core.extraction.enrichers.llm_resolver import (
    _MAX_HASHTAGS_FOR_DISCOVERY,
    LLMResolver,
    _DiscoveredCandidate,
    _ResolvedCandidate,
    _ResolverLocation,
    _ResolverResponse,
    _ResolverTag,
)
from kebi.core.extraction.types import (
    EvidenceField,
    ExtractionContext,
    KnownPlace,
    Medium,
    Producer,
)
from kebi.core.places import TagType
from kebi.providers.llm import InstructorExtraction


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
        instructor.extract = AsyncMock(return_value=InstructorExtraction(data=response))
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
    assert out.queries[normalize_query("1. Mirror Temple")] == ("Wat Phuttha Prommayan")


@pytest.mark.asyncio
async def test_discovers_name_from_caption_with_empty_known_places() -> None:
    """UC1: a venue named only in caption prose — no producer surfaced
    it — is discovered and appended as an LLM_NER KnownPlace."""
    resp = _ResolverResponse(
        discovered=[
            _DiscoveredCandidate(
                name="Thip Samai",
                search_query="Thip Samai Bangkok",
                display_label="Thip Samai",
                found_in=EvidenceField.CAPTION,
            ),
        ],
        location=_ResolverLocation(city="Bangkok"),
    )
    ctx = ExtractionContext(url="https://x.com", user_id="u1")
    ctx.caption = "Best pad thai at Thip Samai 🔥"
    out = await _resolver(resp).resolve(ctx)

    key = normalize_query("Thip Samai")
    assert out.queries[key] == "Thip Samai Bangkok"
    assert out.display_labels[key] == "Thip Samai"
    # Appended to context.known_places as an LLM_NER name producer.
    assert len(ctx.known_places) == 1
    kp = ctx.known_places[0]
    assert kp.name == "Thip Samai"
    assert kp.producer == Producer.LLM_NER
    assert kp.medium == Medium.CAPTION


@pytest.mark.asyncio
async def test_discovers_from_supplementary_text_and_hashtag() -> None:
    """UC8 + venue hashtags: discovery reads supplementary_text and
    hashtags, stamping the matching Medium on each KnownPlace."""
    resp = _ResolverResponse(
        discovered=[
            _DiscoveredCandidate(
                name="Jay Fai",
                search_query="Jay Fai Bangkok",
                found_in=EvidenceField.SUPPLEMENTARY_TEXT,
            ),
            _DiscoveredCandidate(
                name="Thip Samai",
                search_query="Thip Samai Bangkok",
                found_in=EvidenceField.HASHTAG,
            ),
        ],
        location=_ResolverLocation(),
    )
    ctx = ExtractionContext(
        url="https://x.com", user_id="u1", supplementary_text="save Jay Fai"
    )
    ctx.hashtags = ["thipsamaibangkok"]
    out = await _resolver(resp).resolve(ctx)

    media = {kp.name: kp.medium for kp in ctx.known_places}
    assert media == {
        "Jay Fai": Medium.SUPPLEMENTARY_TEXT,
        "Thip Samai": Medium.HASHTAG,
    }
    assert normalize_query("Jay Fai") in out.queries
    assert normalize_query("Thip Samai") in out.queries


@pytest.mark.asyncio
async def test_discovered_name_deduped_against_known_places() -> None:
    """A name a producer already contributed must not be re-appended as
    a duplicate LLM_NER entry — even if the LLM lists it in `discovered`
    without echoing it as a `candidate`."""
    resp = _ResolverResponse(
        candidates=[],  # LLM did not echo "Sorn" as a candidate
        discovered=[
            _DiscoveredCandidate(
                name="Sorn",
                search_query="Sorn Bangkok",
                found_in=EvidenceField.CAPTION,
            ),
        ],
        location=_ResolverLocation(),
    )
    ctx = _ctx(["Sorn"], caption="Loved Sorn")
    await _resolver(resp).resolve(ctx)

    # Still exactly one known_place — the original VISION_IMAGES entry,
    # no LLM_NER duplicate.
    assert len(ctx.known_places) == 1
    assert ctx.known_places[0].producer == Producer.VISION_IMAGES


@pytest.mark.asyncio
async def test_runs_discovery_when_text_present_but_no_known_places() -> None:
    """The LLM call must fire on free text alone — the no-known_places
    early-return only applies when there is also no text."""
    resp = _ResolverResponse(location=_ResolverLocation())
    resolver = _resolver(resp)
    ctx = ExtractionContext(url="https://x.com", user_id="u1")
    ctx.caption = "a day out"

    await resolver.resolve(ctx)

    resolver._instructor_client.extract.assert_awaited_once()


@pytest.mark.asyncio
async def test_hashtag_discovery_gated_on_spray_tagged_post() -> None:
    """Count-gate backstop: a post with too many hashtags has
    hashtag-sourced discovery dropped wholesale; discovery from other
    fields (caption/title/transcript) is unaffected."""
    resp = _ResolverResponse(
        discovered=[
            _DiscoveredCandidate(
                name="Thip Samai",
                search_query="Thip Samai Bangkok",
                found_in=EvidenceField.CAPTION,
            ),
            _DiscoveredCandidate(
                name="Some Mall",
                search_query="Some Mall Bangkok",
                found_in=EvidenceField.HASHTAG,
            ),
        ],
        location=_ResolverLocation(),
    )
    ctx = ExtractionContext(url="https://x.com", user_id="u1")
    ctx.caption = "pad thai at Thip Samai"
    ctx.hashtags = [f"tag{i}" for i in range(_MAX_HASHTAGS_FOR_DISCOVERY + 1)]
    out = await _resolver(resp).resolve(ctx)

    # Caption discovery survives; the hashtag-sourced one is dropped.
    assert {kp.name for kp in ctx.known_places} == {"Thip Samai"}
    assert normalize_query("Thip Samai") in out.queries
    assert normalize_query("Some Mall") not in out.queries


@pytest.mark.asyncio
async def test_per_candidate_area_populates_query_locations() -> None:
    """ADR-082: a candidate with its own `area` gets a per-query
    location override (country inherited from the shared post
    location); a candidate without `area` is absent from the map and
    falls back to the shared location at search time."""
    resp = _ResolverResponse(
        candidates=[
            _ResolvedCandidate(
                raw_name="Inntel Hotel",
                search_query="Inntel Hotel Zaandam",
                area="Zaandam",
            ),
            _ResolvedCandidate(
                raw_name="Rijksmuseum",
                search_query="Rijksmuseum Amsterdam",
            ),
        ],
        location=_ResolverLocation(city="Amsterdam", country="Netherlands"),
    )
    out = await _resolver(resp).resolve(_ctx(["Inntel Hotel", "Rijksmuseum"]))

    inntel = out.query_locations[normalize_query("Inntel Hotel")]
    assert inntel.city == "Zaandam"
    assert inntel.country == "Netherlands"  # inherited from shared post location
    assert normalize_query("Rijksmuseum") not in out.query_locations


@pytest.mark.asyncio
async def test_discovered_candidate_area_populates_query_locations() -> None:
    """A free-text-discovered venue carries its own `area` too."""
    resp = _ResolverResponse(
        discovered=[
            _DiscoveredCandidate(
                name="Doolhof",
                search_query="Doolhof Volendam",
                found_in=EvidenceField.CAPTION,
                area="Volendam",
            ),
        ],
        location=_ResolverLocation(city="Amsterdam", country="Netherlands"),
    )
    ctx = ExtractionContext(url="https://x.com", user_id="u1")
    ctx.caption = "the alley Doolhof, in Volendam"
    out = await _resolver(resp).resolve(ctx)

    loc = out.query_locations[normalize_query("Doolhof")]
    assert loc.city == "Volendam"
    assert loc.country == "Netherlands"
