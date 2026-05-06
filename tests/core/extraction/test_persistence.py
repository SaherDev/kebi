"""Tests for ExtractionPersistenceService (ADR-054 / feature 019).

The persistence service now goes through `PlacesService.create_batch` and
receives `ValidatedCandidate` wrappers (not the removed `ExtractionResult`).
`DuplicatePlaceError` is the signal for "already in DB" — the service
retries conflicting rows one by one so successful rows are not lost.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from totoro_ai.core.events.events import PlaceSaved
from totoro_ai.core.extraction.persistence import (
    ExtractionPersistenceService,
    PlaceSaveOutcome,
)
from totoro_ai.core.extraction.types import ExtractionLevel, ValidatedCandidate
from totoro_ai.core.places import (
    DuplicatePlaceError,
    DuplicateProviderId,
    PlaceAttributes,
    PlaceObject,
    PlaceProvider,
    PlaceType,
)

# ---------------------------------------------------------------------------
# factories
# ---------------------------------------------------------------------------


def _make_validated(
    place_name: str = "Fuji Ramen",
    confidence: float = 0.87,
    resolved_by: ExtractionLevel = ExtractionLevel.LLM_NER,
    external_id: str = "place_123",
    provider: PlaceProvider = PlaceProvider.google,
    cuisine: str | None = "ramen",
    match_lat: float | None = None,
    match_lng: float | None = None,
    match_address: str | None = None,
) -> ValidatedCandidate:
    return ValidatedCandidate(
        place_name=place_name,
        place_type=PlaceType.food_and_drink,
        provider=provider,
        external_id=external_id,
        confidence=confidence,
        resolved_by=resolved_by,
        subcategory="restaurant",
        attributes=PlaceAttributes(cuisine=cuisine),
        corroborated=False,
        match_lat=match_lat,
        match_lng=match_lng,
        match_address=match_address,
    )


def _make_saved_object(
    place_id: str,
    place_name: str = "Fuji Ramen",
    provider_id: str | None = "google:place_123",
) -> PlaceObject:
    return PlaceObject(
        place_id=place_id,
        place_name=place_name,
        place_type=PlaceType.food_and_drink,
        subcategory="restaurant",
        attributes=PlaceAttributes(cuisine="ramen"),
        provider_id=provider_id,
    )


@pytest.fixture
def places_service() -> AsyncMock:
    svc = AsyncMock()
    svc.create_batch = AsyncMock()
    svc.create = AsyncMock()
    svc.get = AsyncMock()
    return svc


@pytest.fixture
def embedding_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.bulk_upsert_embeddings = AsyncMock()
    return repo


@pytest.fixture
def embedder() -> AsyncMock:
    e = AsyncMock()
    e.embed = AsyncMock(return_value=[[0.1] * 1024])
    return e


@pytest.fixture
def event_dispatcher() -> AsyncMock:
    d = AsyncMock()
    d.dispatch = AsyncMock()
    return d


@pytest.fixture
def places_cache() -> AsyncMock:
    c = AsyncMock()
    c.set_geo_batch = AsyncMock()
    c.set_enrichment_batch = AsyncMock()
    return c


@pytest.fixture
def service(
    places_service: AsyncMock,
    places_cache: AsyncMock,
    embedding_repo: AsyncMock,
    embedder: AsyncMock,
    event_dispatcher: AsyncMock,
) -> ExtractionPersistenceService:
    return ExtractionPersistenceService(
        places_service=places_service,
        places_cache=places_cache,
        embedding_repo=embedding_repo,
        embedder=embedder,
        event_dispatcher=event_dispatcher,
    )


# ---------------------------------------------------------------------------
# Happy path — create_batch succeeds
# ---------------------------------------------------------------------------


async def test_new_place_is_saved_and_place_saved_dispatched(
    service: ExtractionPersistenceService,
    places_service: AsyncMock,
    event_dispatcher: AsyncMock,
) -> None:
    places_service.create_batch.return_value = [_make_saved_object("place-1")]

    outcomes = await service.save_and_emit([_make_validated()], user_id="user-1")

    assert len(outcomes) == 1
    assert outcomes[0].status == "saved"
    assert outcomes[0].place_id == "place-1"
    places_service.create_batch.assert_awaited_once()
    event_dispatcher.dispatch.assert_awaited_once()
    dispatched: PlaceSaved = event_dispatcher.dispatch.call_args[0][0]
    assert dispatched.place_ids == ["place-1"]
    assert dispatched.user_id == "user-1"


async def test_returns_list_of_outcomes(
    service: ExtractionPersistenceService,
    places_service: AsyncMock,
) -> None:
    places_service.create_batch.return_value = [_make_saved_object("place-1")]

    outcomes = await service.save_and_emit([_make_validated()], user_id="user-1")

    assert isinstance(outcomes, list)
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], PlaceSaveOutcome)
    assert isinstance(outcomes[0].place_id, str)
    assert len(outcomes[0].place_id) > 0


# ---------------------------------------------------------------------------
# Duplicate handling — via DuplicatePlaceError from PlacesService
# ---------------------------------------------------------------------------


async def test_all_duplicates_marked_and_no_place_saved_event(
    service: ExtractionPersistenceService,
    places_service: AsyncMock,
    event_dispatcher: AsyncMock,
) -> None:
    """When create_batch raises DuplicatePlaceError listing every row, all
    outcomes are marked "duplicate" and PlaceSaved is NOT dispatched."""
    vcs = [
        _make_validated("Place A", external_id="ext_a"),
        _make_validated("Place B", external_id="ext_b"),
    ]
    places_service.create_batch.side_effect = DuplicatePlaceError(
        [
            DuplicateProviderId(
                provider_id="google:ext_a", existing_place_id="existing-a"
            ),
            DuplicateProviderId(
                provider_id="google:ext_b", existing_place_id="existing-b"
            ),
        ]
    )
    places_service.get.side_effect = [
        _make_saved_object("existing-a", "Place A", "google:ext_a"),
        _make_saved_object("existing-b", "Place B", "google:ext_b"),
    ]

    outcomes = await service.save_and_emit(vcs, user_id="user-1")

    assert all(o.status == "duplicate" for o in outcomes)
    assert {o.place_id for o in outcomes} == {"existing-a", "existing-b"}
    event_dispatcher.dispatch.assert_not_awaited()


async def test_partial_duplicate_retries_one_by_one(
    service: ExtractionPersistenceService,
    places_service: AsyncMock,
    event_dispatcher: AsyncMock,
) -> None:
    """When only some rows are duplicates, non-conflicting rows are retried
    via create() so successful rows are not lost."""
    vcs = [
        _make_validated("Place A", external_id="ext_a"),
        _make_validated("Place B", external_id="ext_b"),
    ]
    places_service.create_batch.side_effect = DuplicatePlaceError(
        [
            DuplicateProviderId(
                provider_id="google:ext_b", existing_place_id="existing-b"
            )
        ]
    )
    # Retry loop: B is looked up as duplicate; A is re-created successfully.
    places_service.get.return_value = _make_saved_object(
        "existing-b", "Place B", "google:ext_b"
    )
    places_service.create.return_value = _make_saved_object(
        "new-a", "Place A", "google:ext_a"
    )

    outcomes = await service.save_and_emit(vcs, user_id="user-1")

    statuses = [o.status for o in outcomes]
    assert statuses == ["saved", "duplicate"]
    assert outcomes[0].place_id == "new-a"
    assert outcomes[1].place_id == "existing-b"


# ---------------------------------------------------------------------------
# Confidence bands — ADR-057: three-way partition
#   c < save_threshold (0.30)             → below_threshold, never written
#   save_threshold ≤ c < confident (0.70) → needs_review, written + flagged
#   c ≥ confident_threshold (0.70)        → saved silently
# ---------------------------------------------------------------------------


async def test_below_threshold_is_not_saved(
    service: ExtractionPersistenceService,
    places_service: AsyncMock,
    event_dispatcher: AsyncMock,
) -> None:
    """Confidence below save_threshold (0.30) is not written to DB."""
    vc = _make_validated(confidence=0.25)

    outcomes = await service.save_and_emit([vc], user_id="user-1")

    assert outcomes[0].status == "below_threshold"
    assert outcomes[0].place_id is None
    places_service.create_batch.assert_not_awaited()
    event_dispatcher.dispatch.assert_not_awaited()


async def test_at_confident_threshold_is_saved(
    service: ExtractionPersistenceService,
    places_service: AsyncMock,
) -> None:
    """Confidence exactly at confident_threshold (0.70) is a silent save."""
    places_service.create_batch.return_value = [_make_saved_object("place-1")]
    vc = _make_validated(confidence=0.70)

    outcomes = await service.save_and_emit([vc], user_id="user-1")

    assert outcomes[0].status == "saved"
    places_service.create_batch.assert_awaited_once()


async def test_all_below_threshold_no_place_saved_event(
    service: ExtractionPersistenceService,
    places_service: AsyncMock,
    event_dispatcher: AsyncMock,
) -> None:
    """Every candidate below save_threshold (0.30) → nothing written, no event."""
    outcomes = await service.save_and_emit(
        [
            _make_validated("A", confidence=0.20, external_id="ext_a"),
            _make_validated("B", confidence=0.28, external_id="ext_b"),
        ],
        user_id="user-1",
    )

    assert all(o.status == "below_threshold" for o in outcomes)
    places_service.create_batch.assert_not_awaited()
    event_dispatcher.dispatch.assert_not_awaited()


async def test_needs_review_band_is_saved_with_flag(
    service: ExtractionPersistenceService,
    places_service: AsyncMock,
    event_dispatcher: AsyncMock,
    embedder: AsyncMock,
    embedding_repo: AsyncMock,
) -> None:
    """Confidence in [save_threshold, confident_threshold) is saved as
    'needs_review': written to the repo, embedded, and a PlaceSaved event
    is dispatched — but the status flags the row as user-confirmable."""
    places_service.create_batch.return_value = [_make_saved_object("place-1")]
    embedder.embed = AsyncMock(return_value=[[0.1] * 1024])
    vc = _make_validated(confidence=0.60)

    outcomes = await service.save_and_emit([vc], user_id="user-1")

    assert outcomes[0].status == "needs_review"
    assert outcomes[0].place is not None
    assert outcomes[0].place_id == "place-1"
    places_service.create_batch.assert_awaited_once()
    event_dispatcher.dispatch.assert_awaited_once()
    embedding_repo.bulk_upsert_embeddings.assert_awaited_once()


async def test_three_band_partition_mixed_batch(
    service: ExtractionPersistenceService,
    places_service: AsyncMock,
    event_dispatcher: AsyncMock,
    embedder: AsyncMock,
) -> None:
    """A mixed batch yields one saved, one needs_review, one below_threshold.

    Both eligible bands share the single create_batch call and the single
    PlaceSaved event, carrying both place_ids."""
    places_service.create_batch.return_value = [
        _make_saved_object("p-confident", "Confident Place"),
        _make_saved_object(
            "p-tentative", "Tentative Place", provider_id="google:ext_t"
        ),
    ]
    embedder.embed = AsyncMock(return_value=[[0.1] * 1024, [0.2] * 1024])
    vcs = [
        _make_validated("Confident Place", external_id="ext_c", confidence=0.90),
        _make_validated("Tentative Place", external_id="ext_t", confidence=0.55),
        _make_validated("Junk", external_id="ext_j", confidence=0.20),
    ]

    outcomes = await service.save_and_emit(vcs, user_id="user-1")

    statuses = [o.status for o in outcomes]
    assert statuses == ["saved", "needs_review", "below_threshold"]

    # Both write-band rows share the single create_batch call.
    places_service.create_batch.assert_awaited_once()
    items_arg = places_service.create_batch.call_args[0][0]
    assert len(items_arg) == 2

    # One event, both saved_ids present (below_threshold excluded).
    event_dispatcher.dispatch.assert_awaited_once()
    event: PlaceSaved = event_dispatcher.dispatch.call_args[0][0]
    assert set(event.place_ids) == {"p-confident", "p-tentative"}


# ---------------------------------------------------------------------------
# Batch embedding
# ---------------------------------------------------------------------------


async def test_single_place_calls_bulk_upsert_with_one_record(
    service: ExtractionPersistenceService,
    places_service: AsyncMock,
    embedding_repo: AsyncMock,
    embedder: AsyncMock,
) -> None:
    places_service.create_batch.return_value = [_make_saved_object("place-1")]
    embedder.embed = AsyncMock(return_value=[[0.1] * 1024])

    await service.save_and_emit([_make_validated()], user_id="user-1")

    embedder.embed.assert_awaited_once()
    texts = embedder.embed.call_args[0][0]
    assert len(texts) == 1

    embedding_repo.bulk_upsert_embeddings.assert_awaited_once()
    records = embedding_repo.bulk_upsert_embeddings.call_args[0][0]
    assert len(records) == 1


async def test_multi_place_calls_bulk_upsert_with_all_records(
    service: ExtractionPersistenceService,
    places_service: AsyncMock,
    embedding_repo: AsyncMock,
    embedder: AsyncMock,
) -> None:
    places_service.create_batch.return_value = [
        _make_saved_object(f"place-{i}", f"Place {i}") for i in range(5)
    ]
    embedder.embed = AsyncMock(return_value=[[0.1] * 1024] * 5)
    vcs = [_make_validated(f"Place {i}", external_id=f"ext_{i}") for i in range(5)]

    outcomes = await service.save_and_emit(vcs, user_id="user-1")

    assert len(outcomes) == 5
    assert all(o.status == "saved" for o in outcomes)
    texts = embedder.embed.call_args[0][0]
    assert len(texts) == 5
    embedder.embed.assert_awaited_once()

    embedding_repo.bulk_upsert_embeddings.assert_awaited_once()
    records = embedding_repo.bulk_upsert_embeddings.call_args[0][0]
    assert len(records) == 5


async def test_embedding_failure_is_non_fatal(
    service: ExtractionPersistenceService,
    places_service: AsyncMock,
    embedding_repo: AsyncMock,
    event_dispatcher: AsyncMock,
) -> None:
    places_service.create_batch.return_value = [_make_saved_object("place-1")]
    embedding_repo.bulk_upsert_embeddings = AsyncMock(
        side_effect=RuntimeError("DB error")
    )

    outcomes = await service.save_and_emit([_make_validated()], user_id="user-1")

    assert len(outcomes) == 1
    assert outcomes[0].status == "saved"
    event_dispatcher.dispatch.assert_awaited_once()


# ---------------------------------------------------------------------------
# Ordering invariant: DB writes → dispatch → embeddings
# ---------------------------------------------------------------------------


async def test_place_saved_dispatched_before_embeddings(
    service: ExtractionPersistenceService,
    places_service: AsyncMock,
    event_dispatcher: AsyncMock,
    embedding_repo: AsyncMock,
) -> None:
    places_service.create_batch.return_value = [_make_saved_object("place-1")]
    call_order: list[str] = []

    async def dispatch_side_effect(event: object) -> None:
        call_order.append("dispatch")

    async def embed_side_effect(records: list) -> None:
        call_order.append("embed_bulk")

    event_dispatcher.dispatch = AsyncMock(side_effect=dispatch_side_effect)
    embedding_repo.bulk_upsert_embeddings = AsyncMock(side_effect=embed_side_effect)

    await service.save_and_emit([_make_validated()], user_id="user-1")

    assert call_order == ["dispatch", "embed_bulk"]


# ---------------------------------------------------------------------------
# Multi-place: one PlaceSaved with all saved IDs only
# ---------------------------------------------------------------------------


async def test_multiple_places_one_place_saved_event_with_all_ids(
    service: ExtractionPersistenceService,
    places_service: AsyncMock,
    event_dispatcher: AsyncMock,
    embedder: AsyncMock,
) -> None:
    places_service.create_batch.return_value = [
        _make_saved_object("p-a", "Place A"),
        _make_saved_object("p-b", "Place B"),
    ]
    embedder.embed = AsyncMock(return_value=[[0.1] * 1024, [0.2] * 1024])
    vcs = [
        _make_validated("Place A", external_id="ext_a"),
        _make_validated("Place B", external_id="ext_b"),
    ]

    outcomes = await service.save_and_emit(vcs, user_id="user-1")

    saved_ids = [o.place_id for o in outcomes if o.status == "saved"]
    event_dispatcher.dispatch.assert_awaited_once()
    event: PlaceSaved = event_dispatcher.dispatch.call_args[0][0]
    assert set(event.place_ids) == set(saved_ids)
    assert len(event.place_ids) == 2


# ---------------------------------------------------------------------------
# Geo cache write — Tier 2 data from Google validation lands in Redis
# ---------------------------------------------------------------------------


async def test_geo_cache_written_for_saved_rows_with_full_match_data(
    service: ExtractionPersistenceService,
    places_service: AsyncMock,
    places_cache: AsyncMock,
) -> None:
    """When the validator returned lat/lng/address, persistence writes
    them to the Tier 2 cache keyed by provider_id."""
    places_service.create_batch.return_value = [
        _make_saved_object("p1", provider_id="google:ext_a")
    ]
    vc = _make_validated(
        "Place A",
        external_id="ext_a",
        match_lat=13.7563,
        match_lng=100.5018,
        match_address="1 Sukhumvit Rd, Bangkok, Thailand",
    )

    await service.save_and_emit([vc], user_id="user-1")

    places_cache.set_geo_batch.assert_awaited_once()
    geo_arg = places_cache.set_geo_batch.call_args[0][0]
    assert set(geo_arg.keys()) == {"google:ext_a"}
    entry = geo_arg["google:ext_a"]
    assert entry.lat == 13.7563
    assert entry.lng == 100.5018
    assert entry.address == "1 Sukhumvit Rd, Bangkok, Thailand"


async def test_geo_cache_skipped_when_match_address_missing(
    service: ExtractionPersistenceService,
    places_service: AsyncMock,
    places_cache: AsyncMock,
) -> None:
    """Partial geo data (lat/lng but no address) is not written — a
    GeoData entry must be complete to be useful downstream."""
    places_service.create_batch.return_value = [
        _make_saved_object("p1", provider_id="google:ext_a")
    ]
    vc = _make_validated(
        "Place A",
        external_id="ext_a",
        match_lat=13.7563,
        match_lng=100.5018,
        match_address=None,
    )

    await service.save_and_emit([vc], user_id="user-1")

    places_cache.set_geo_batch.assert_not_awaited()


async def test_geo_cache_skipped_for_below_threshold_rows(
    service: ExtractionPersistenceService,
    places_service: AsyncMock,
    places_cache: AsyncMock,
) -> None:
    """Below-threshold rows never reach Tier 1, so nothing to cache either."""
    vc = _make_validated(
        "Place A",
        confidence=0.20,
        match_lat=13.7563,
        match_lng=100.5018,
        match_address="Bangkok",
    )

    await service.save_and_emit([vc], user_id="user-1")

    places_cache.set_geo_batch.assert_not_awaited()
    places_service.create_batch.assert_not_awaited()


async def test_geo_cache_written_for_needs_review_rows(
    service: ExtractionPersistenceService,
    places_service: AsyncMock,
    places_cache: AsyncMock,
) -> None:
    """Tentative saves also get their geo cached — they're Tier 1 rows
    that recall should surface with geo_fresh=True."""
    places_service.create_batch.return_value = [
        _make_saved_object("p1", provider_id="google:ext_a")
    ]
    vc = _make_validated(
        "Place A",
        confidence=0.55,
        external_id="ext_a",
        match_lat=13.7563,
        match_lng=100.5018,
        match_address="Bangkok",
    )

    outcomes = await service.save_and_emit([vc], user_id="user-1")

    assert outcomes[0].status == "needs_review"
    places_cache.set_geo_batch.assert_awaited_once()


# ---------------------------------------------------------------------------
# Source / source_url stamping
# ---------------------------------------------------------------------------


async def test_source_url_and_source_stamped_on_place_create(
    service: ExtractionPersistenceService,
    places_service: AsyncMock,
) -> None:
    """source_url and source passed in to save_and_emit are stamped onto
    every PlaceCreate handed to places_service.create_batch."""
    from totoro_ai.core.places import PlaceSource

    places_service.create_batch.return_value = [_make_saved_object("p1")]
    vc = _make_validated()

    await service.save_and_emit(
        [vc],
        user_id="user-1",
        source_url="https://www.tiktok.com/@user/video/123",
        source=PlaceSource.tiktok,
    )

    places_service.create_batch.assert_awaited_once()
    items = places_service.create_batch.call_args[0][0]
    assert items[0].source_url == "https://www.tiktok.com/@user/video/123"
    assert items[0].source == PlaceSource.tiktok


async def test_mixed_saved_and_below_threshold_only_saved_in_event(
    service: ExtractionPersistenceService,
    places_service: AsyncMock,
    event_dispatcher: AsyncMock,
    embedder: AsyncMock,
) -> None:
    """Confident save + below-threshold drop: only the confident place_id
    lands in the PlaceSaved event."""
    places_service.create_batch.return_value = [
        _make_saved_object("p-good", "Good Place")
    ]
    embedder.embed = AsyncMock(return_value=[[0.1] * 1024])
    vcs = [
        _make_validated("Good Place", external_id="ext_good", confidence=0.85),
        _make_validated("Junk", external_id="ext_junk", confidence=0.20),
    ]

    outcomes = await service.save_and_emit(vcs, user_id="user-1")

    saved = [o for o in outcomes if o.status == "saved"]
    below = [o for o in outcomes if o.status == "below_threshold"]
    assert len(saved) == 1
    assert len(below) == 1

    event_dispatcher.dispatch.assert_awaited_once()
    event: PlaceSaved = event_dispatcher.dispatch.call_args[0][0]
    assert len(event.place_ids) == 1
    assert event.place_ids[0] == saved[0].place_id
