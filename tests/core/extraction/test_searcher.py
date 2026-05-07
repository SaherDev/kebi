"""Tests for PlacesSearcher and reconcile_picks."""

from unittest.mock import AsyncMock, MagicMock

from kebi.core.config import ConfidenceConfig
from kebi.core.extraction.searcher import (
    PlacesSearcher,
    reconcile_picks,
)
from kebi.core.extraction.types import (
    Evidence,
    ExtractionContext,
    KnownPlace,
    Medium,
    Producer,
    SearchMatch,
    ValidatedCandidate,
)
from kebi.core.places import (
    PlaceAttributes,
    PlaceProvider,
    PlacesMatchQuality,
    PlacesMatchResult,
    PlaceType,
)


def _ctx_with_names(
    names: list[str], location_tag: str | None = None
) -> ExtractionContext:
    ctx = ExtractionContext(url=None, user_id="u1")
    for n in names:
        ctx.known_places.append(
            KnownPlace(
                name=n,
                producer=Producer.GOOGLE_MAPS_LIST,
                medium=Medium.LIST,
                snippet=n,
            )
        )
    if location_tag:
        ctx.location_tag = location_tag
    return ctx


def _exact_match(
    name: str, external_id: str = "place_abc"
) -> PlacesMatchResult:
    return PlacesMatchResult(
        match_quality=PlacesMatchQuality.EXACT,
        validated_name=name,
        external_provider="google",
        external_id=external_id,
        lat=13.7,
        lng=100.5,
        address=f"Sukhumvit, {name}",
        place_types=["restaurant", "food"],
    )


def _none_match() -> PlacesMatchResult:
    return PlacesMatchResult(match_quality=PlacesMatchQuality.NONE)


def _category_match() -> PlacesMatchResult:
    return PlacesMatchResult(
        match_quality=PlacesMatchQuality.CATEGORY_ONLY,
        validated_name="Generic Cafe",
        external_id="ChIJ_cat",
        place_types=["cafe"],
    )


def _geographic_match(name: str = "Bangkok") -> PlacesMatchResult:
    return PlacesMatchResult(
        match_quality=PlacesMatchQuality.EXACT,
        validated_name=name,
        external_id="ChIJ_loc",
        place_types=["locality", "political"],
    )


# ---------------------------------------------------------------------------
# PlacesSearcher
# ---------------------------------------------------------------------------


async def test_searcher_fans_out_one_call_per_known_place() -> None:
    client = MagicMock()
    client.validate_place = AsyncMock(
        side_effect=[
            _exact_match("Joe's Pizza", external_id="id_1"),
            _exact_match("Eleven Madison", external_id="id_2"),
        ]
    )
    searcher = PlacesSearcher(places_client=client)
    ctx = _ctx_with_names(["Joe's Pizza", "Eleven Madison"])

    await searcher.search(ctx)

    assert client.validate_place.await_count == 2
    assert len(ctx.search_matches) == 2
    assert {m.external_id for m in ctx.search_matches} == {"id_1", "id_2"}


async def test_searcher_dedups_duplicate_query_strings() -> None:
    """Two known_places with the same normalized name → one Google call."""
    client = MagicMock()
    client.validate_place = AsyncMock(
        return_value=_exact_match("Joe's Pizza", external_id="id_1")
    )
    searcher = PlacesSearcher(places_client=client)
    ctx = _ctx_with_names(["Joe's Pizza", "joe's pizza", "JOE'S PIZZA"])

    await searcher.search(ctx)

    assert client.validate_place.await_count == 1
    assert len(ctx.search_matches) == 1


async def test_searcher_drops_none_quality() -> None:
    client = MagicMock()
    client.validate_place = AsyncMock(return_value=_none_match())
    searcher = PlacesSearcher(places_client=client)
    ctx = _ctx_with_names(["Made-up Place"])

    await searcher.search(ctx)

    assert ctx.search_matches == []


async def test_searcher_drops_category_only_quality() -> None:
    client = MagicMock()
    client.validate_place = AsyncMock(return_value=_category_match())
    searcher = PlacesSearcher(places_client=client)
    ctx = _ctx_with_names(["Generic Cafe"])

    await searcher.search(ctx)

    assert ctx.search_matches == []


async def test_searcher_drops_geographic_place_types() -> None:
    """Matches resolving to localities/countries/streets are dropped."""
    client = MagicMock()
    client.validate_place = AsyncMock(return_value=_geographic_match())
    searcher = PlacesSearcher(places_client=client)
    ctx = _ctx_with_names(["Bangkok"])

    await searcher.search(ctx)

    assert ctx.search_matches == []


async def test_searcher_skips_already_searched_queries_across_levels() -> None:
    """Calling search() again with new known_places must not re-query the
    names searched on the previous level."""
    client = MagicMock()
    client.validate_place = AsyncMock(
        side_effect=[
            _exact_match("Joe's Pizza", external_id="id_1"),
            _exact_match("New Spot", external_id="id_2"),
        ]
    )
    searcher = PlacesSearcher(places_client=client)
    ctx = _ctx_with_names(["Joe's Pizza"])

    await searcher.search(ctx)
    assert client.validate_place.await_count == 1

    # Deep level adds a new vision-detected name.
    ctx.known_places.append(
        KnownPlace(
            name="New Spot",
            producer=Producer.VISION_FRAMES,
            medium=Medium.FRAME,
        )
    )
    await searcher.search(ctx)

    # Joe's Pizza was not re-queried; only New Spot.
    assert client.validate_place.await_count == 2
    assert len(ctx.search_matches) == 2


async def test_searcher_uses_location_tag_as_query() -> None:
    """When no producers contributed names but location_tag is set, the
    searcher still runs one query — sometimes the tag itself matches a
    venue (rare) but more importantly it's used as the query."""
    client = MagicMock()
    client.validate_place = AsyncMock(
        return_value=_exact_match("Sukhumvit Soi 11", external_id="id_loc")
    )
    searcher = PlacesSearcher(places_client=client)
    ctx = ExtractionContext(url=None, user_id="u1")
    ctx.location_tag = "Sukhumvit Soi 11"

    await searcher.search(ctx)

    assert client.validate_place.await_count == 1
    args = client.validate_place.call_args
    assert args.kwargs.get("name") == "Sukhumvit Soi 11" or (
        args.args and args.args[0] == "Sukhumvit Soi 11"
    )


async def test_searcher_passes_location_tag_as_location_hint() -> None:
    """validate_place(name, location=) — the location_tag rides as hint."""
    client = MagicMock()
    client.validate_place = AsyncMock(
        return_value=_exact_match("Joe's Pizza", external_id="id_1")
    )
    searcher = PlacesSearcher(places_client=client)
    ctx = _ctx_with_names(["Joe's Pizza"], location_tag="Bangkok")

    await searcher.search(ctx)

    kwargs = client.validate_place.call_args.kwargs
    assert kwargs.get("location") == "Bangkok"


async def test_searcher_failure_logs_and_skips() -> None:
    """A per-query exception drops only that query, not the rest."""
    client = MagicMock()
    client.validate_place = AsyncMock(
        side_effect=[
            Exception("boom"),
            _exact_match("Surviving", external_id="id_keep"),
        ]
    )
    searcher = PlacesSearcher(places_client=client)
    ctx = _ctx_with_names(["Boom Place", "Surviving"])

    await searcher.search(ctx)

    assert len(ctx.search_matches) == 1
    assert ctx.search_matches[0].external_id == "id_keep"


# ---------------------------------------------------------------------------
# reconcile_picks
# ---------------------------------------------------------------------------


def _search_match(
    external_id: str = "id_1",
    validated_name: str = "Joe's Pizza",
    address: str | None = "Bangkok",
) -> SearchMatch:
    return SearchMatch(
        query=validated_name,
        query_producer=Producer.GOOGLE_MAPS_LIST,
        query_medium=Medium.LIST,
        validated_name=validated_name,
        provider=PlaceProvider.google,
        external_id=external_id,
        match_quality=PlacesMatchQuality.EXACT,
        lat=13.7,
        lng=100.5,
        address=address,
    )


def _pick(
    external_id: str = "id_1",
    place_name: str = "ignored",
    evidence: list[Evidence] | None = None,
) -> ValidatedCandidate:
    return ValidatedCandidate(
        place_name=place_name,
        place_type=PlaceType.food_and_drink,
        provider=PlaceProvider.manual,  # will be overridden
        external_id=external_id,
        confidence=0.0,  # will be overridden
        evidence=evidence or [Evidence(Producer.LLM_NER, Medium.CAPTION)],
        attributes=PlaceAttributes(),
    )


def test_reconcile_drops_picks_with_unknown_external_id() -> None:
    """Defense in depth — pick referencing an ID not in search_matches."""
    pick = _pick(external_id="hallucinated_id")
    out = reconcile_picks(
        picks=[pick],
        search_matches=[_search_match(external_id="real_id")],
        confidence_config=ConfidenceConfig(),
        context=ExtractionContext(url=None, user_id="u1"),
    )
    assert out == []


def test_reconcile_overrides_name_and_address_from_search_match() -> None:
    """Picker echoed back wrong values; Google's are canonical."""
    pick = _pick(external_id="id_1", place_name="Wrong Name")
    match = _search_match(
        external_id="id_1",
        validated_name="Joe's Pizza",
        address="Real Address, Bangkok",
    )

    out = reconcile_picks(
        picks=[pick],
        search_matches=[match],
        confidence_config=ConfidenceConfig(),
        context=ExtractionContext(url=None, user_id="u1"),
    )

    assert len(out) == 1
    assert out[0].place_name == "Joe's Pizza"
    assert out[0].match_address == "Real Address, Bangkok"
    assert out[0].match_lat == 13.7


def test_reconcile_sources_provider_from_search_match() -> None:
    """The picker's provider field is ignored — Google wins."""
    pick = _pick()
    pick.provider = PlaceProvider.manual  # picker emitted wrong
    match = _search_match()

    out = reconcile_picks(
        picks=[pick],
        search_matches=[match],
        confidence_config=ConfidenceConfig(),
        context=ExtractionContext(url=None, user_id="u1"),
    )

    assert out[0].provider == PlaceProvider.google


def test_reconcile_computes_confidence_from_evidence_and_match_quality() -> None:
    """Confidence is recomputed; the picker's value is ignored."""
    pick = _pick(external_id="id_1")
    pick.confidence = 9999.0  # nonsense — should be overwritten
    match = _search_match(external_id="id_1")

    out = reconcile_picks(
        picks=[pick],
        search_matches=[match],
        confidence_config=ConfidenceConfig(),
        context=ExtractionContext(url=None, user_id="u1"),
    )

    assert 0.0 < out[0].confidence < 1.0


def test_reconcile_attaches_search_side_producer_to_evidence() -> None:
    """The SearchMatch's query_producer is stamped onto the picked
    candidate's evidence so vision/list provenance survives."""
    pick = _pick(
        external_id="id_1",
        evidence=[Evidence(Producer.LLM_NER, Medium.CAPTION)],
    )
    match = SearchMatch(
        query="Joe's Pizza",
        query_producer=Producer.VISION_FRAMES,
        query_medium=Medium.FRAME,
        validated_name="Joe's Pizza",
        provider=PlaceProvider.google,
        external_id="id_1",
        match_quality=PlacesMatchQuality.EXACT,
    )

    out = reconcile_picks(
        picks=[pick],
        search_matches=[match],
        confidence_config=ConfidenceConfig(),
        context=ExtractionContext(url=None, user_id="u1"),
    )

    producers = {e.producer for e in out[0].evidence}
    assert Producer.VISION_FRAMES in producers
    assert Producer.LLM_NER in producers
