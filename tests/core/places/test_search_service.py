"""Tests for PlacesSearchService — warm path, cold path, stale refresh."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from kebi.core.places.models import (
    LocationContext,
    NonVenueDetection,
    PlaceCategory,
    PlaceCore,
    PlaceObject,
    PlaceQuery,
    PlaceTag,
)
from kebi.core.places.search_service import PlacesSearchService
from kebi.core.places.tags import CuisineTag

# Marker for "this object came through the cache/provider path" — the
# observable trace the overlay leaves now that the live half is gone.
_CACHED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _make_service(
    repo: MagicMock | None = None,
    cache: MagicMock | None = None,
    client: MagicMock | None = None,
    upsert_service: MagicMock | None = None,
) -> PlacesSearchService:
    repo = repo or MagicMock(
        find=AsyncMock(return_value=[]),
        get_by_provider_ids=AsyncMock(return_value={}),
    )
    cache = cache or MagicMock(
        mget=AsyncMock(return_value={}),
        mset=AsyncMock(),
    )
    client = client or MagicMock(
        search=AsyncMock(return_value=[]),
        get_by_ids=AsyncMock(return_value=[]),
    )
    upsert_service = upsert_service or MagicMock(
        upsert_and_embed=AsyncMock(return_value=[]),
    )
    return PlacesSearchService(
        repo=repo,
        cache=cache,
        client=client,
        upsert_service=upsert_service,
    )


def _core(pid: str, lat: float | None = 1.0) -> PlaceCore:
    return PlaceCore(
        id=pid,
        provider_id=f"google:{pid}",
        place_name=f"Place {pid}",
        location=(
            LocationContext(lat=lat, address="Test St") if lat is not None else None
        ),
    )


def _object(pid: str) -> PlaceObject:
    return PlaceObject(
        id=pid,
        provider_id=f"google:{pid}",
        place_name=f"Place {pid}",
        location=LocationContext(lat=1.0, address="Test St"),
        cached_at=_CACHED_AT,
    )


def _idless_object(pid: str) -> PlaceObject:
    """A provider-fetched object exactly as the client returns it: a
    provider_id but no catalog id (the row is only assigned one on persist).
    This is the shape that produced the null-`place.id` bug."""
    return PlaceObject(
        id=None,
        provider_id=f"google:{pid}",
        place_name=f"Place {pid}",
        location=LocationContext(lat=1.0, address="Test St"),
        cached_at=_CACHED_AT,
    )


def _nameless_object(pid: str) -> PlaceObject:
    """A Place Details fetch as the client returns it post-ADR-118: the
    details mask omits displayName, so place_name arrives empty and the
    search service must backfill it from the catalog row."""
    return PlaceObject(
        id=None,
        provider_id=f"google:{pid}",
        place_name="",
        location=LocationContext(lat=1.0, address="Test St"),
        cached_at=_CACHED_AT,
    )


# ---------------------------------------------------------------------------
# Warm path
# ---------------------------------------------------------------------------


class TestWarmPath:
    async def test_returns_db_hits_with_cache_overlay(self) -> None:
        cores = [_core("a"), _core("b"), _core("c")]
        cached_obj = _object("b")
        repo = MagicMock(
            find=AsyncMock(return_value=cores),
            get_by_provider_ids=AsyncMock(return_value={}),
        )
        cache = MagicMock(mget=AsyncMock(return_value={"google:b": cached_obj}))
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[]),
        )

        svc = _make_service(repo=repo, cache=cache, client=client)
        results = await svc.find(PlaceQuery(), limit=20)

        assert len(results) == 3
        b_result = next(r for r in results if r.provider_id == "google:b")
        assert b_result.cached_at == _CACHED_AT
        # warm path — no Google call
        client.search.assert_not_awaited()


# ---------------------------------------------------------------------------
# Cold path (Google fallback)
# ---------------------------------------------------------------------------


class TestColdPath:
    async def test_falls_back_to_google_when_db_empty(self) -> None:
        google_result = _object("g1")
        repo = MagicMock(
            find=AsyncMock(return_value=[]),
            get_by_provider_ids=AsyncMock(return_value={}),
        )
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(
            search=AsyncMock(return_value=[google_result]),
            get_by_ids=AsyncMock(return_value=[]),
        )
        upsert = MagicMock(upsert_and_embed=AsyncMock(return_value=[_core("g1")]))

        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )
        results = await svc.find(
            PlaceQuery(place_names=["Thai restaurants Bangkok"]), limit=5
        )

        client.search.assert_awaited_once()
        upsert.upsert_and_embed.assert_awaited_once()
        cache.mset.assert_awaited_once()
        assert results == [google_result]

    async def test_passes_full_query_to_client_search(self) -> None:
        """Service passes the PlaceQuery unchanged — client owns routing."""
        repo = MagicMock(find=AsyncMock(return_value=[]))
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[]),
        )
        q = PlaceQuery(
            tags=[CuisineTag.thai],
            location=LocationContext(lat=13.7, lng=100.5, radius_m=500),
        )
        svc = _make_service(repo=repo, client=client)
        await svc.find(q, limit=10)

        client.search.assert_awaited_once()
        passed_query: PlaceQuery = client.search.call_args.args[0]
        assert passed_query is q

    async def test_empty_google_result_returns_empty(self) -> None:
        repo = MagicMock(find=AsyncMock(return_value=[]))
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[]),
        )
        svc = _make_service(repo=repo, client=client)
        results = await svc.find(PlaceQuery())

        assert results == []

    async def test_cold_path_skips_persist_on_empty(self) -> None:
        """Empty Google response → no upsert, no mset."""
        repo = MagicMock(find=AsyncMock(return_value=[]))
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(search=AsyncMock(return_value=[]))
        upsert = MagicMock(upsert_and_embed=AsyncMock(return_value=[]))
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )
        await svc.find(PlaceQuery(place_names=["ghost town"]))

        upsert.upsert_and_embed.assert_not_awaited()
        cache.mset.assert_not_awaited()

    async def test_cold_path_persists_then_returns_results(self) -> None:
        """Multiple Google results → batch upsert + batch mset, full results out."""
        results_in = [_object("g1"), _object("g2"), _object("g3")]
        repo = MagicMock(find=AsyncMock(return_value=[]))
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(search=AsyncMock(return_value=results_in))
        upsert = MagicMock(upsert_and_embed=AsyncMock(return_value=[]))
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )
        results = await svc.find(PlaceQuery(place_names=["busy"]))

        upsert.upsert_and_embed.assert_awaited_once()
        cache.mset.assert_awaited_once_with(results_in)
        upsert_arg = upsert.upsert_and_embed.call_args.args[0]
        assert len(upsert_arg) == 3
        assert results == results_in


# ---------------------------------------------------------------------------
# Cold-path catalog-id reconciliation — the freshly-discovered place must
# carry the id the upsert just minted, not escape with id=None.
# ---------------------------------------------------------------------------


class TestColdPathIdReconciliation:
    async def test_cold_path_stamps_minted_id_onto_result(self) -> None:
        """A Google object arrives with id=None; the upsert mints a catalog id;
        find() returns the object carrying that id (keyed by provider_id)."""
        google_result = _idless_object("g1")
        repo = MagicMock(
            find=AsyncMock(return_value=[]),
            get_by_provider_ids=AsyncMock(return_value={}),
        )
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(
            search=AsyncMock(return_value=[google_result]),
            get_by_ids=AsyncMock(return_value=[]),
        )
        upsert = MagicMock(
            upsert_and_embed=AsyncMock(return_value=[_core("g1")]),
        )
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )

        results = await svc.find(PlaceQuery(place_names=["cafe"]), limit=5)

        assert len(results) == 1
        assert results[0].id == "g1"  # was None on the way in
        assert results[0].provider_id == "google:g1"

    async def test_cold_path_caches_stamped_objects(self) -> None:
        """The cache is warmed with id-bearing objects, not the id-less ones,
        so later cache hits carry the id too."""
        repo = MagicMock(
            find=AsyncMock(return_value=[]),
            get_by_provider_ids=AsyncMock(return_value={}),
        )
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(
            search=AsyncMock(return_value=[_idless_object("g1")]),
            get_by_ids=AsyncMock(return_value=[]),
        )
        upsert = MagicMock(
            upsert_and_embed=AsyncMock(return_value=[_core("g1")]),
        )
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )

        await svc.find(PlaceQuery(place_names=["cafe"]), limit=5)

        cached = cache.mset.call_args.args[0]
        assert [p.id for p in cached] == ["g1"]

    async def test_cold_path_matches_by_provider_id_not_position(self) -> None:
        """Upsert RETURNING order is not guaranteed — ids must be matched by
        provider_id. A shuffled upsert return still stamps correctly."""
        results_in = [_idless_object("a"), _idless_object("b")]
        repo = MagicMock(
            find=AsyncMock(return_value=[]),
            get_by_provider_ids=AsyncMock(return_value={}),
        )
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(
            search=AsyncMock(return_value=results_in),
            get_by_ids=AsyncMock(return_value=[]),
        )
        # Returned in reverse order relative to the input.
        upsert = MagicMock(
            upsert_and_embed=AsyncMock(return_value=[_core("b"), _core("a")]),
        )
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )

        results = await svc.find(PlaceQuery(place_names=["x"]), limit=5)

        by_provider = {r.provider_id: r.id for r in results}
        assert by_provider == {"google:a": "a", "google:b": "b"}

    async def test_get_by_ids_stamps_fetched_objects(self) -> None:
        """The by-id cold branch reconciles the same way: a fetched id-less
        object comes back carrying its catalog id."""
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(
            get_by_ids=AsyncMock(return_value=[_idless_object("b")]),
        )
        upsert = MagicMock(
            upsert_and_embed=AsyncMock(return_value=[_core("b")]),
        )
        svc = _make_service(cache=cache, client=client, upsert_service=upsert)

        result = await svc.get_by_ids(["google:b"])

        assert result["google:b"].id == "b"


# ---------------------------------------------------------------------------
# Stale-row handling in find()
# ---------------------------------------------------------------------------


class TestFindEnrichment:
    async def test_full_cache_hit_skips_provider(self) -> None:
        """When every DB hit is in cache, no provider call is made."""
        repo = MagicMock(
            find=AsyncMock(return_value=[_core("a"), _core("b")]),
            get_by_provider_ids=AsyncMock(return_value={}),
        )
        cache = MagicMock(
            mget=AsyncMock(
                return_value={
                    "google:a": _object("a"),
                    "google:b": _object("b"),
                }
            ),
            mset=AsyncMock(),
        )
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[]),
        )
        svc = _make_service(repo=repo, cache=cache, client=client)
        await svc.find(PlaceQuery(), limit=20)

        cache.mget.assert_awaited_once()
        client.get_by_ids.assert_not_awaited()
        cache.mset.assert_not_awaited()

    async def test_stale_row_falls_back_to_provider(self) -> None:
        """A stale row → cache miss → client.get_by_ids → upsert + mset."""
        stale = _core("stale", lat=None)
        fresh = _core("fresh")
        repo = MagicMock(
            find=AsyncMock(return_value=[stale, fresh]),
            get_by_provider_ids=AsyncMock(return_value={}),
        )
        cache = MagicMock(
            mget=AsyncMock(return_value={"google:fresh": _object("fresh")}),
            mset=AsyncMock(),
        )
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[_object("stale")]),
        )
        upsert = MagicMock(upsert_and_embed=AsyncMock(return_value=[]))
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )
        await svc.find(PlaceQuery(), limit=20)

        client.get_by_ids.assert_awaited_once_with(["google:stale"])
        upsert.upsert_and_embed.assert_awaited_once()
        cache.mset.assert_awaited_once()

    async def test_partial_cache_hit_fetches_only_missing(self) -> None:
        """No staleness, partial cache: provider asked only for misses."""
        repo = MagicMock(
            find=AsyncMock(return_value=[_core("a"), _core("b"), _core("c")]),
            get_by_provider_ids=AsyncMock(return_value={}),
        )
        cache = MagicMock(
            mget=AsyncMock(return_value={"google:a": _object("a")}),
            mset=AsyncMock(),
        )
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[_object("b"), _object("c")]),
        )
        upsert = MagicMock(upsert_and_embed=AsyncMock(return_value=[]))
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )
        results = await svc.find(PlaceQuery(), limit=20)

        passed = client.get_by_ids.call_args.args[0]
        assert set(passed) == {"google:b", "google:c"}
        assert len(results) == 3
        assert all(r.cached_at == _CACHED_AT for r in results)

    async def test_overlay_takes_location_from_cache(self) -> None:
        """Cache location overrides DB location even when DB has lat/lng."""
        db_core = _core("a", lat=10.0)  # DB location lat=10
        cached = _object("a").model_copy(
            update={
                "location": LocationContext(
                    lat=42.42, lng=-71.0, address="Cache Ave", city="Boston"
                )
            }
        )
        repo = MagicMock(find=AsyncMock(return_value=[db_core]))
        cache = MagicMock(
            mget=AsyncMock(return_value={"google:a": cached}),
            mset=AsyncMock(),
        )
        svc = _make_service(repo=repo, cache=cache)
        results = await svc.find(PlaceQuery(), limit=5)

        assert results[0].location is not None
        assert results[0].location.lat == 42.42
        assert results[0].location.address == "Cache Ave"
        assert results[0].location.city == "Boston"

    async def test_overlay_propagates_cached_at(self) -> None:
        """The cache entry's cached_at flows into the result."""
        stamp = datetime(2026, 3, 15, tzinfo=UTC)
        cached = _object("a").model_copy(update={"cached_at": stamp})
        repo = MagicMock(find=AsyncMock(return_value=[_core("a")]))
        cache = MagicMock(
            mget=AsyncMock(return_value={"google:a": cached}),
            mset=AsyncMock(),
        )
        svc = _make_service(repo=repo, cache=cache)
        result = (await svc.find(PlaceQuery(), limit=5))[0]

        assert result.cached_at == stamp

    async def test_db_authoritative_for_curated_fields(self) -> None:
        """DB place_name wins over a cache copy with a different (stale) name."""
        db_core = _core("a")
        # cache holds an older / divergent name (e.g., before user-curated rename).
        cached = _object("a").model_copy(update={"place_name": "Old Name"})
        repo = MagicMock(find=AsyncMock(return_value=[db_core]))
        cache = MagicMock(
            mget=AsyncMock(return_value={"google:a": cached}),
            mset=AsyncMock(),
        )
        svc = _make_service(repo=repo, cache=cache)
        result = (await svc.find(PlaceQuery(), limit=5))[0]

        assert result.place_name == "Place a"  # from DB core

    async def test_core_without_provider_id_returned_bare(self) -> None:
        """A core without provider_id can't be enriched; emit it as-is."""
        anonymous = PlaceCore(id="z", provider_id=None, place_name="Anonymous")
        repo = MagicMock(find=AsyncMock(return_value=[anonymous]))
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[]),
        )
        svc = _make_service(repo=repo, cache=cache, client=client)
        results = await svc.find(PlaceQuery(), limit=5)

        client.get_by_ids.assert_not_awaited()
        assert len(results) == 1
        assert results[0].provider_id is None
        assert results[0].cached_at is None

    async def test_stale_row_unresolvable_by_provider(self) -> None:
        """Stale row + cache miss + provider returns nothing → bare core out."""
        stale = _core("ghost", lat=None)
        repo = MagicMock(find=AsyncMock(return_value=[stale]))
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[]),
        )
        upsert = MagicMock(upsert_and_embed=AsyncMock(return_value=[]))
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )
        results = await svc.find(PlaceQuery(), limit=5)

        client.get_by_ids.assert_awaited_once_with(["google:ghost"])
        upsert.upsert_and_embed.assert_not_awaited()
        cache.mset.assert_not_awaited()
        assert len(results) == 1
        assert results[0].location is None
        assert results[0].cached_at is None


# ---------------------------------------------------------------------------
# Post-TTL recovery — DB location wiped + cache expired.
# ---------------------------------------------------------------------------


class TestPostTTLRecovery:
    async def test_full_recovery_db_wiped_cache_empty(self) -> None:
        """Post-30-day-cron: DB lat=None, cache empty. Provider repopulates both
        and the result carries the fresh location."""
        wiped = _core("p1", lat=None)
        fresh = _object("p1").model_copy(
            update={
                "location": LocationContext(
                    lat=13.7, lng=100.5, address="Sukhumvit Soi 11"
                ),
            }
        )
        repo = MagicMock(find=AsyncMock(return_value=[wiped]))
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[fresh]),
        )
        upsert = MagicMock(upsert_and_embed=AsyncMock(return_value=[]))
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )
        results = await svc.find(PlaceQuery(), limit=5)

        client.get_by_ids.assert_awaited_once_with(["google:p1"])
        upsert.upsert_and_embed.assert_awaited_once()
        cache.mset.assert_awaited_once_with([fresh])
        assert results[0].location is not None
        assert results[0].location.lat == 13.7
        assert results[0].location.address == "Sukhumvit Soi 11"
        assert results[0].cached_at == _CACHED_AT

    async def test_db_fully_null_location_treated_stale(self) -> None:
        """LocationContext entirely None → counted as stale → provider call."""
        no_loc = PlaceCore(
            id="p2",
            provider_id="google:p2",
            place_name="Place p2",
            location=None,
        )
        repo = MagicMock(find=AsyncMock(return_value=[no_loc]))
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[_object("p2")]),
        )
        upsert = MagicMock(upsert_and_embed=AsyncMock(return_value=[]))
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )
        await svc.find(PlaceQuery(), limit=5)

        client.get_by_ids.assert_awaited_once_with(["google:p2"])

    async def test_lat_none_lng_present_treated_stale(self) -> None:
        """The staleness check keys on lat — lat=None alone triggers refresh."""
        partial = PlaceCore(
            id="p3",
            provider_id="google:p3",
            place_name="Place p3",
            location=LocationContext(lat=None, lng=100.5, address="Half"),
        )
        repo = MagicMock(find=AsyncMock(return_value=[partial]))
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[]),
        )
        svc = _make_service(repo=repo, cache=cache, client=client)
        await svc.find(PlaceQuery(), limit=5)

        client.get_by_ids.assert_awaited_once_with(["google:p3"])

    async def test_lng_none_lat_present_NOT_treated_stale(self) -> None:
        """Asymmetry: staleness check keys on lat only — lng=None alone is not
        considered stale. With a cache miss, get_by_ids still calls the
        provider (because cache misses always do), but it's the cache miss
        driving it, not staleness. This pins the current behavior so a
        future change to the staleness predicate is a conscious decision."""
        # Cache returns a hit so we can isolate the staleness signal.
        partial = PlaceCore(
            id="p4",
            provider_id="google:p4",
            place_name="Place p4",
            location=LocationContext(lat=1.0, lng=None, address="Half-lng"),
        )
        repo = MagicMock(find=AsyncMock(return_value=[partial]))
        cache = MagicMock(
            mget=AsyncMock(return_value={"google:p4": _object("p4")}),
            mset=AsyncMock(),
        )
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[]),
        )
        svc = _make_service(repo=repo, cache=cache, client=client)
        await svc.find(PlaceQuery(), limit=5)

        # No provider call — cache hit covers it, and lng=None alone is not
        # currently considered a staleness trigger.
        client.get_by_ids.assert_not_awaited()

    async def test_db_stale_but_cache_warm_uses_cache_location(self) -> None:
        """DB lat=None but cache still warm: cache fills the location, no
        provider call needed (cache is the source of truth for location)."""
        wiped = _core("p4", lat=None)
        cached = _object("p4").model_copy(
            update={
                "location": LocationContext(
                    lat=40.0, lng=-74.0, address="Manhattan", city="NYC"
                )
            }
        )
        repo = MagicMock(find=AsyncMock(return_value=[wiped]))
        cache = MagicMock(
            mget=AsyncMock(return_value={"google:p4": cached}),
            mset=AsyncMock(),
        )
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[]),
        )
        svc = _make_service(repo=repo, cache=cache, client=client)
        results = await svc.find(PlaceQuery(), limit=5)

        # Stale row triggers get_by_ids routing, but cache hit means no provider call.
        client.get_by_ids.assert_not_awaited()
        assert results[0].location is not None
        assert results[0].location.lat == 40.0
        assert results[0].location.city == "NYC"


# ---------------------------------------------------------------------------
# DB-vs-cache divergence: which side wins for each field.
# ---------------------------------------------------------------------------


class TestFieldOwnership:
    async def test_db_wins_for_curated_fields(self) -> None:
        """name, aliases, tags, categories come from DB even when cache differs."""
        db_core = PlaceCore(
            id="x",
            provider_id="google:x",
            place_name="DB Name",
            categories=[PlaceCategory.cafe],
            tags=[PlaceTag(type="cuisine", value="thai", source="manual")],
            location=LocationContext(lat=1.0, address="DB"),
        )
        cached = _object("x").model_copy(
            update={
                "place_name": "Cache Name",
                "categories": [PlaceCategory.restaurant],
                "tags": [PlaceTag(type="cuisine", value="italian", source="google")],
            }
        )
        repo = MagicMock(find=AsyncMock(return_value=[db_core]))
        cache = MagicMock(
            mget=AsyncMock(return_value={"google:x": cached}),
            mset=AsyncMock(),
        )
        svc = _make_service(repo=repo, cache=cache)
        result = (await svc.find(PlaceQuery(), limit=5))[0]

        assert result.place_name == "DB Name"
        assert result.categories == [PlaceCategory.cafe]
        assert [t.value for t in result.tags] == ["thai"]

    def test_legacy_cache_payload_with_live_fields_still_parses(self) -> None:
        """Cache entries written before ADR-118 carry the dropped live fields
        (rating, hours, phone, ...). They must still deserialize — Pydantic
        ignores unknown keys — so no cache flush is needed on deploy."""
        legacy_payload = {
            "id": "a",
            "provider_id": "google:a",
            "place_name": "Place a",
            "location": {"lat": 1.0, "address": "Test St"},
            "cached_at": "2026-01-01T00:00:00+00:00",
            "rating": 4.5,
            "hours": {"timezone": "Asia/Bangkok", "monday": ["09:00-22:00"]},
            "phone": "+66-2-555-0000",
            "website": "https://example.test",
            "popularity": 1234,
            "business_status": "operational",
        }
        obj = PlaceObject.model_validate(legacy_payload)

        assert obj.provider_id == "google:a"
        assert obj.cached_at == _CACHED_AT
        assert not hasattr(obj, "rating")

    async def test_no_cache_entry_yields_bare_object(self) -> None:
        """No cache entry → cached_at None, core fields preserved."""
        repo = MagicMock(find=AsyncMock(return_value=[_core("z")]))
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[]),
        )
        svc = _make_service(repo=repo, cache=cache, client=client)
        result = (await svc.find(PlaceQuery(), limit=5))[0]

        assert result.place_name == "Place z"
        assert result.cached_at is None


# ---------------------------------------------------------------------------
# find() — query passthrough and ordering invariants.
# ---------------------------------------------------------------------------


class TestFindContract:
    async def test_limit_forwarded_to_repo(self) -> None:
        """The `limit` arg propagates verbatim to repo.find."""
        repo = MagicMock(find=AsyncMock(return_value=[]))
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(search=AsyncMock(return_value=[]))
        svc = _make_service(repo=repo, cache=cache, client=client)
        await svc.find(PlaceQuery(), limit=42)

        repo.find.assert_awaited_once()
        _, kwargs = repo.find.call_args
        passed_limit = (
            repo.find.call_args.args[1]
            if len(repo.find.call_args.args) > 1
            else kwargs.get("limit")
        )
        assert passed_limit == 42

    async def test_query_forwarded_to_repo(self) -> None:
        """The PlaceQuery instance is passed unchanged to repo.find."""
        q = PlaceQuery(
            tags=[CuisineTag.thai],
            location=LocationContext(lat=13.7, lng=100.5, radius_m=500),
        )
        repo = MagicMock(find=AsyncMock(return_value=[]))
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(search=AsyncMock(return_value=[]))
        svc = _make_service(repo=repo, cache=cache, client=client)
        await svc.find(q, limit=10)

        repo.find.assert_awaited_once()
        assert repo.find.call_args.args[0] is q

    async def test_result_order_matches_db_order(self) -> None:
        """Cache lookup must not reorder DB hits — the repo's sort is preserved."""
        cores = [_core("c"), _core("a"), _core("b")]
        repo = MagicMock(find=AsyncMock(return_value=cores))
        cache = MagicMock(
            mget=AsyncMock(
                return_value={
                    "google:a": _object("a"),
                    "google:b": _object("b"),
                    "google:c": _object("c"),
                }
            ),
            mset=AsyncMock(),
        )
        svc = _make_service(repo=repo, cache=cache)
        results = await svc.find(PlaceQuery(), limit=10)

        assert [r.provider_id for r in results] == [
            "google:c",
            "google:a",
            "google:b",
        ]

    async def test_mixed_stale_and_fresh_with_no_cache(self) -> None:
        """Stale + fresh DB rows, all cache-missed: provider asked for both,
        result spans both, persist is batched in one upsert + one mset."""
        stale = _core("s", lat=None)
        fresh = _core("f")
        repo = MagicMock(find=AsyncMock(return_value=[stale, fresh]))
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[_object("s"), _object("f")]),
        )
        upsert = MagicMock(upsert_and_embed=AsyncMock(return_value=[]))
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )
        results = await svc.find(PlaceQuery(), limit=10)

        passed = client.get_by_ids.call_args.args[0]
        assert set(passed) == {"google:s", "google:f"}
        upsert.upsert_and_embed.assert_awaited_once()
        cache.mset.assert_awaited_once()
        assert len(results) == 2
        assert {r.provider_id for r in results} == {"google:s", "google:f"}

    async def test_external_fallback_only_on_db_empty(self) -> None:
        """No external calls (search OR get_by_ids) when DB hits and cache is warm."""
        repo = MagicMock(find=AsyncMock(return_value=[_core("a")]))
        cache = MagicMock(
            mget=AsyncMock(return_value={"google:a": _object("a")}),
            mset=AsyncMock(),
        )
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[]),
        )
        upsert = MagicMock(upsert_and_embed=AsyncMock(return_value=[]))
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )
        await svc.find(PlaceQuery(place_names=["x"]), limit=5)

        client.search.assert_not_awaited()
        client.get_by_ids.assert_not_awaited()
        upsert.upsert_and_embed.assert_not_awaited()
        cache.mset.assert_not_awaited()


# ---------------------------------------------------------------------------
# get_by_ids
# ---------------------------------------------------------------------------


class TestGetByIds:
    async def test_full_cache_hit_skips_provider(self) -> None:
        cached = {"google:a": _object("a"), "google:b": _object("b")}
        cache = MagicMock(mget=AsyncMock(return_value=cached), mset=AsyncMock())
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[]),
        )
        svc = _make_service(cache=cache, client=client)

        result = await svc.get_by_ids(["google:a", "google:b"])

        cache.mget.assert_awaited_once_with(["google:a", "google:b"])
        client.get_by_ids.assert_not_awaited()
        cache.mset.assert_not_awaited()
        assert set(result) == {"google:a", "google:b"}

    async def test_cache_miss_falls_back_to_provider(self) -> None:
        """Misses are fetched, upserted, cached, and merged with hits."""
        cached = {"google:a": _object("a")}
        fetched = _object("b")
        cache = MagicMock(mget=AsyncMock(return_value=cached), mset=AsyncMock())
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[fetched]),
        )
        upsert = MagicMock(upsert_and_embed=AsyncMock(return_value=[]))
        svc = _make_service(cache=cache, client=client, upsert_service=upsert)

        result = await svc.get_by_ids(["google:a", "google:b"])

        client.get_by_ids.assert_awaited_once_with(["google:b"])
        upsert.upsert_and_embed.assert_awaited_once()
        cache.mset.assert_awaited_once_with([fetched])
        assert result["google:a"].provider_id == "google:a"
        assert result["google:b"] is fetched

    async def test_unresolvable_id_absent_from_result(self) -> None:
        """Ids the provider can't resolve are simply omitted from the result."""
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[]),
        )
        upsert = MagicMock(upsert_and_embed=AsyncMock(return_value=[]))
        svc = _make_service(cache=cache, client=client, upsert_service=upsert)

        result = await svc.get_by_ids(["google:ghost"])

        client.get_by_ids.assert_awaited_once_with(["google:ghost"])
        upsert.upsert_and_embed.assert_not_awaited()
        cache.mset.assert_not_awaited()
        assert result == {}

    async def test_empty_input(self) -> None:
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(get_by_ids=AsyncMock(return_value=[]))
        svc = _make_service(cache=cache, client=client)
        assert await svc.get_by_ids([]) == {}
        cache.mget.assert_not_awaited()
        client.get_by_ids.assert_not_awaited()

    async def test_single_id_cache_hit(self) -> None:
        cached = {"google:s": _object("s")}
        cache = MagicMock(mget=AsyncMock(return_value=cached), mset=AsyncMock())
        client = MagicMock(get_by_ids=AsyncMock(return_value=[]))
        svc = _make_service(cache=cache, client=client)

        result = await svc.get_by_ids(["google:s"])

        client.get_by_ids.assert_not_awaited()
        assert result["google:s"].provider_id == "google:s"

    async def test_single_id_cache_miss_fetches_one(self) -> None:
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(get_by_ids=AsyncMock(return_value=[_object("s")]))
        upsert = MagicMock(upsert_and_embed=AsyncMock(return_value=[]))
        svc = _make_service(cache=cache, client=client, upsert_service=upsert)

        result = await svc.get_by_ids(["google:s"])

        client.get_by_ids.assert_awaited_once_with(["google:s"])
        upsert.upsert_and_embed.assert_awaited_once()
        cache.mset.assert_awaited_once()
        assert "google:s" in result

    async def test_only_misses_passed_to_provider(self) -> None:
        """The provider call carries exactly the missing ids, in order."""
        cached = {"google:b": _object("b"), "google:d": _object("d")}
        cache = MagicMock(mget=AsyncMock(return_value=cached), mset=AsyncMock())
        client = MagicMock(get_by_ids=AsyncMock(return_value=[]))
        upsert = MagicMock(upsert_and_embed=AsyncMock(return_value=[]))
        svc = _make_service(cache=cache, client=client, upsert_service=upsert)

        await svc.get_by_ids(["google:a", "google:b", "google:c", "google:d"])

        passed = client.get_by_ids.call_args.args[0]
        assert passed == ["google:a", "google:c"]

    async def test_partial_provider_resolution(self) -> None:
        """Provider returns a strict subset of the misses; absent ids drop out."""
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(
            get_by_ids=AsyncMock(return_value=[_object("a"), _object("c")])
        )
        upsert = MagicMock(upsert_and_embed=AsyncMock(return_value=[]))
        svc = _make_service(cache=cache, client=client, upsert_service=upsert)

        result = await svc.get_by_ids(["google:a", "google:b", "google:c"])

        assert set(result) == {"google:a", "google:c"}
        cache.mset.assert_awaited_once()
        msetted = cache.mset.call_args.args[0]
        assert {p.provider_id for p in msetted} == {"google:a", "google:c"}

    async def test_persist_writes_in_single_batch(self) -> None:
        """All fetched results upserted in one upsert_and_embed + one mset."""
        fetched = [_object(p) for p in ("a", "b", "c")]
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(get_by_ids=AsyncMock(return_value=fetched))
        upsert = MagicMock(upsert_and_embed=AsyncMock(return_value=[]))
        svc = _make_service(cache=cache, client=client, upsert_service=upsert)

        await svc.get_by_ids(["google:a", "google:b", "google:c"])

        upsert.upsert_and_embed.assert_awaited_once()
        upsert_arg = upsert.upsert_and_embed.call_args.args[0]
        assert len(upsert_arg) == 3
        cache.mset.assert_awaited_once_with(fetched)

    async def test_cache_hit_with_sparse_object_still_treated_as_hit(self) -> None:
        """Cache returning a PlaceObject with location=None is still a hit;
        we don't second-guess the cache by re-fetching."""
        partial = _object("a").model_copy(update={"location": None})
        cache = MagicMock(
            mget=AsyncMock(return_value={"google:a": partial}),
            mset=AsyncMock(),
        )
        client = MagicMock(get_by_ids=AsyncMock(return_value=[]))
        svc = _make_service(cache=cache, client=client)

        result = await svc.get_by_ids(["google:a"])

        client.get_by_ids.assert_not_awaited()
        assert result["google:a"].location is None


class TestDetailsNameBackfill:
    """ADR-118: details fetches arrive nameless; the DB row is the name
    authority. Nameless objects with no catalog row must never be
    persisted or cached."""

    async def test_nameless_fetch_backfills_db_name(self) -> None:
        repo = MagicMock(
            find=AsyncMock(return_value=[]),
            get_by_provider_ids=AsyncMock(return_value={"google:b": _core("b")}),
        )
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(get_by_ids=AsyncMock(return_value=[_nameless_object("b")]))
        upsert = MagicMock(upsert_and_embed=AsyncMock(return_value=[_core("b")]))
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )

        result = await svc.get_by_ids(["google:b"])

        assert result["google:b"].place_name == "Place b"
        # Persisted and cached with the backfilled name, not the empty one.
        persisted = upsert.upsert_and_embed.call_args.args[0]
        assert persisted[0].place_name == "Place b"
        cached_objs = cache.mset.call_args.args[0]
        assert cached_objs[0].place_name == "Place b"

    async def test_nameless_fetch_without_db_row_is_dropped(self) -> None:
        repo = MagicMock(
            find=AsyncMock(return_value=[]),
            get_by_provider_ids=AsyncMock(return_value={}),
        )
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(get_by_ids=AsyncMock(return_value=[_nameless_object("x")]))
        upsert = MagicMock(upsert_and_embed=AsyncMock(return_value=[]))
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )

        result = await svc.get_by_ids(["google:x"])

        assert result == {}
        upsert.upsert_and_embed.assert_not_awaited()
        cache.mset.assert_not_awaited()

    async def test_named_fetch_skips_repo_lookup(self) -> None:
        repo = MagicMock(
            find=AsyncMock(return_value=[]),
            get_by_provider_ids=AsyncMock(return_value={}),
        )
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(get_by_ids=AsyncMock(return_value=[_object("a")]))
        upsert = MagicMock(upsert_and_embed=AsyncMock(return_value=[]))
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )

        result = await svc.get_by_ids(["google:a"])

        repo.get_by_provider_ids.assert_not_awaited()
        assert result["google:a"].place_name == "Place a"


# ---------------------------------------------------------------------------
# get_cores_by_ids — DB-only analytical read (ADR-077)
# ---------------------------------------------------------------------------


class TestGetCoresByIds:
    async def test_empty_input_returns_empty(self) -> None:
        svc = _make_service()
        assert await svc.get_cores_by_ids([]) == {}

    async def test_db_only_keyed_by_internal_id(self) -> None:
        repo = MagicMock(
            find=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[_core("a"), _core("b")]),
        )
        cache = MagicMock(mget=AsyncMock(), mset=AsyncMock())
        client = MagicMock(search=AsyncMock(), get_by_ids=AsyncMock())
        svc = _make_service(repo=repo, cache=cache, client=client)

        out = await svc.get_cores_by_ids(["a", "b"])

        assert set(out) == {"a", "b"}
        assert out["a"].id == "a"
        repo.get_by_ids.assert_awaited_once_with(["a", "b"])
        # No cache overlay, no provider fallback.
        cache.mget.assert_not_awaited()
        client.get_by_ids.assert_not_awaited()
        client.search.assert_not_awaited()


class TestDistanceSortThreading:
    """sort_by survives unchanged into BOTH the DB and the Google paths."""

    _DIST_QUERY = PlaceQuery(
        place_names=["Some Bank"],
        sort_by="distance",
        location=LocationContext(lat=13.7, lng=100.5, radius_m=500),
    )

    async def test_db_path_receives_distance_sort(self) -> None:
        repo = MagicMock(
            find=AsyncMock(return_value=[_core("a")]),
            get_by_provider_ids=AsyncMock(return_value={}),
        )
        svc = _make_service(repo=repo)
        await svc.find(self._DIST_QUERY, limit=1)
        assert repo.find.call_args.args[0].sort_by == "distance"

    async def test_google_fallback_receives_distance_sort(self) -> None:
        # DB empty → cold path hits the provider client.
        repo = MagicMock(
            find=AsyncMock(return_value=[]),
            get_by_provider_ids=AsyncMock(return_value={}),
        )
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[]),
        )
        svc = _make_service(repo=repo, client=client)
        await svc.find(self._DIST_QUERY, limit=1)
        client.search.assert_awaited_once()
        assert client.search.call_args.args[0].sort_by == "distance"


class TestIconHintStamping:
    """icon_hint rides the cold-path write-through (ADR-117)."""

    async def test_cold_path_stamps_icon_hint_before_persist(self) -> None:
        repo = MagicMock(
            find=AsyncMock(return_value=[]),
            get_by_provider_ids=AsyncMock(return_value={}),
        )
        client = MagicMock(
            search=AsyncMock(return_value=[_idless_object("g1")]),
            get_by_ids=AsyncMock(return_value=[]),
        )
        upsert = MagicMock(upsert_and_embed=AsyncMock(return_value=[_core("g1")]))
        svc = _make_service(repo=repo, client=client, upsert_service=upsert)

        results = await svc.find(
            PlaceQuery(place_names=["Dubai Fountain"], icon_hint="⛲"), limit=1
        )

        persisted_cores = upsert.upsert_and_embed.call_args.args[0]
        assert persisted_cores[0].icon == "⛲"
        assert results[0].icon == "⛲"

    async def test_icon_hint_does_not_override_provider_icon(self) -> None:
        provider_obj = _idless_object("g1").model_copy(update={"icon": "🍜"})
        client = MagicMock(
            search=AsyncMock(return_value=[provider_obj]),
            get_by_ids=AsyncMock(return_value=[]),
        )
        repo = MagicMock(
            find=AsyncMock(return_value=[]),
            get_by_provider_ids=AsyncMock(return_value={}),
        )
        upsert = MagicMock(upsert_and_embed=AsyncMock(return_value=[_core("g1")]))
        svc = _make_service(repo=repo, client=client, upsert_service=upsert)

        await svc.find(PlaceQuery(place_names=["Ramen Bar"], icon_hint="⛲"), limit=1)

        persisted_cores = upsert.upsert_and_embed.call_args.args[0]
        assert persisted_cores[0].icon == "🍜"

    async def test_warm_path_ignores_icon_hint(self) -> None:
        # DB hit → no cold path, no write with the hint. The stored icon
        # (None here) is what comes back; fill-only happens on cold only.
        repo = MagicMock(
            find=AsyncMock(return_value=[_core("a")]),
            get_by_provider_ids=AsyncMock(return_value={}),
        )
        upsert = MagicMock(upsert_and_embed=AsyncMock(return_value=[]))
        svc = _make_service(repo=repo, upsert_service=upsert)

        results = await svc.find(
            PlaceQuery(place_names=["Place a"], icon_hint="⛲"), limit=1
        )

        upsert.upsert_and_embed.assert_not_awaited()
        assert results[0].icon is None


class TestFindWithRejections:
    """Location-kinds Step 1: the cold path reports the non-venue geography
    the provider search rejected; warm/DB paths report none."""

    @staticmethod
    def _detection() -> NonVenueDetection:
        return NonVenueDetection(
            name="Ha Giang Loop",
            provider_id="google:ChIJloop",
            reason="non_venue_geography",
        )

    async def test_cold_path_propagates_detections(self) -> None:
        detection = self._detection()

        async def _search(
            query: PlaceQuery,
            limit: int = 20,
            *,
            rejections: list[NonVenueDetection] | None = None,
        ) -> list[PlaceObject]:
            if rejections is not None:
                rejections.append(detection)
            return []

        client = MagicMock(
            search=AsyncMock(side_effect=_search),
            get_by_ids=AsyncMock(return_value=[]),
        )
        service = _make_service(client=client)
        places, rejections = await service.find_with_rejections(
            PlaceQuery(place_names=["Ha Giang Loop"])
        )
        assert places == []
        assert rejections == [detection]

    async def test_db_hit_path_reports_no_detections(self) -> None:
        repo = MagicMock(
            find=AsyncMock(return_value=[_core("p1")]),
            get_by_provider_ids=AsyncMock(return_value={}),
        )
        cache = MagicMock(
            mget=AsyncMock(return_value={"google:p1": _object("p1")}),
            mset=AsyncMock(),
        )
        service = _make_service(repo=repo, cache=cache)
        places, rejections = await service.find_with_rejections(
            PlaceQuery(place_names=["Place p1"])
        )
        assert [p.provider_id for p in places] == ["google:p1"]
        assert rejections == []

    async def test_find_keeps_list_shape(self) -> None:
        """`find()` delegates but still returns a bare list for the
        agent-tool callers that never see detections."""
        service = _make_service()
        result = await service.find(PlaceQuery(place_names=["x"]))
        assert result == []
