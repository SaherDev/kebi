"""Tests for PlacesSearchService — warm path, cold path, stale refresh."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

from totoro_ai.core.places_v2.models import (
    HoursDict,
    LocationContext,
    PlaceCategory,
    PlaceCore,
    PlaceObject,
    PlaceQuery,
    PlaceTag,
)
from totoro_ai.core.places_v2.search_service import PlacesSearchService
from totoro_ai.core.places_v2.tags import CuisineTag


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
        upsert_many=AsyncMock(return_value=[]),
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
        rating=4.5,
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
        assert b_result.rating == 4.5
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
        upsert = MagicMock(upsert_many=AsyncMock(return_value=[_core("g1")]))

        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )
        results = await svc.find(
            PlaceQuery(place_name="Thai restaurants Bangkok"), limit=5
        )

        client.search.assert_awaited_once()
        upsert.upsert_many.assert_awaited_once()
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
        upsert = MagicMock(upsert_many=AsyncMock(return_value=[]))
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )
        await svc.find(PlaceQuery(place_name="ghost town"))

        upsert.upsert_many.assert_not_awaited()
        cache.mset.assert_not_awaited()

    async def test_cold_path_persists_then_returns_results(self) -> None:
        """Multiple Google results → batch upsert + batch mset, full results out."""
        results_in = [_object("g1"), _object("g2"), _object("g3")]
        repo = MagicMock(find=AsyncMock(return_value=[]))
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(search=AsyncMock(return_value=results_in))
        upsert = MagicMock(upsert_many=AsyncMock(return_value=[]))
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )
        results = await svc.find(PlaceQuery(place_name="busy"))

        upsert.upsert_many.assert_awaited_once()
        cache.mset.assert_awaited_once_with(results_in)
        upsert_arg = upsert.upsert_many.call_args.args[0]
        assert len(upsert_arg) == 3
        assert results == results_in


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
        upsert = MagicMock(upsert_many=AsyncMock(return_value=[]))
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )
        await svc.find(PlaceQuery(), limit=20)

        client.get_by_ids.assert_awaited_once_with(["google:stale"])
        upsert.upsert_many.assert_awaited_once()
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
            get_by_ids=AsyncMock(
                return_value=[_object("b"), _object("c")]
            ),
        )
        upsert = MagicMock(upsert_many=AsyncMock(return_value=[]))
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )
        results = await svc.find(PlaceQuery(), limit=20)

        passed = client.get_by_ids.call_args.args[0]
        assert set(passed) == {"google:b", "google:c"}
        assert len(results) == 3
        assert all(r.rating == 4.5 for r in results)

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

    async def test_overlay_propagates_all_live_fields(self) -> None:
        """Cache rating/hours/phone/website/popularity flow into result."""
        hours = cast(
            HoursDict,
            {
                "timezone": "Asia/Bangkok",
                "monday": ["09:00-22:00"],
                "tuesday": [],
                "wednesday": [],
                "thursday": [],
                "friday": [],
                "saturday": [],
                "sunday": [],
            },
        )
        cached = _object("a").model_copy(
            update={
                "rating": 4.9,
                "hours": hours,
                "phone": "+66-2-555-0000",
                "website": "https://example.test",
                "popularity": 1234,
            }
        )
        repo = MagicMock(find=AsyncMock(return_value=[_core("a")]))
        cache = MagicMock(
            mget=AsyncMock(return_value={"google:a": cached}),
            mset=AsyncMock(),
        )
        svc = _make_service(repo=repo, cache=cache)
        result = (await svc.find(PlaceQuery(), limit=5))[0]

        assert result.rating == 4.9
        assert result.hours == hours
        assert result.phone == "+66-2-555-0000"
        assert result.website == "https://example.test"
        assert result.popularity == 1234

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
        assert results[0].rating is None

    async def test_stale_row_unresolvable_by_provider(self) -> None:
        """Stale row + cache miss + provider returns nothing → bare core out."""
        stale = _core("ghost", lat=None)
        repo = MagicMock(find=AsyncMock(return_value=[stale]))
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[]),
        )
        upsert = MagicMock(upsert_many=AsyncMock(return_value=[]))
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )
        results = await svc.find(PlaceQuery(), limit=5)

        client.get_by_ids.assert_awaited_once_with(["google:ghost"])
        upsert.upsert_many.assert_not_awaited()
        cache.mset.assert_not_awaited()
        assert len(results) == 1
        assert results[0].location is None
        assert results[0].rating is None


# ---------------------------------------------------------------------------
# Post-TTL recovery — DB location wiped + cache expired.
# ---------------------------------------------------------------------------

class TestPostTTLRecovery:
    async def test_full_recovery_db_wiped_cache_empty(self) -> None:
        """Post-30-day-cron: DB lat=None, cache empty. Provider repopulates both
        and the result has fresh location + live fields."""
        wiped = _core("p1", lat=None)
        fresh = _object("p1").model_copy(
            update={
                "location": LocationContext(
                    lat=13.7, lng=100.5, address="Sukhumvit Soi 11"
                ),
                "rating": 4.7,
            }
        )
        repo = MagicMock(find=AsyncMock(return_value=[wiped]))
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[fresh]),
        )
        upsert = MagicMock(upsert_many=AsyncMock(return_value=[]))
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )
        results = await svc.find(PlaceQuery(), limit=5)

        client.get_by_ids.assert_awaited_once_with(["google:p1"])
        upsert.upsert_many.assert_awaited_once()
        cache.mset.assert_awaited_once_with([fresh])
        assert results[0].location is not None
        assert results[0].location.lat == 13.7
        assert results[0].location.address == "Sukhumvit Soi 11"
        assert results[0].rating == 4.7

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
        upsert = MagicMock(upsert_many=AsyncMock(return_value=[]))
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
        """name, aliases, tags, category come from DB even when cache differs."""
        db_core = PlaceCore(
            id="x",
            provider_id="google:x",
            place_name="DB Name",
            category=PlaceCategory.cafe,
            tags=[PlaceTag(type="cuisine", value="thai", source="manual")],
            location=LocationContext(lat=1.0, address="DB"),
        )
        cached = _object("x").model_copy(
            update={
                "place_name": "Cache Name",
                "category": PlaceCategory.restaurant,
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
        assert result.category == PlaceCategory.cafe
        assert [t.value for t in result.tags] == ["thai"]

    async def test_cache_wins_for_live_fields(self) -> None:
        """rating/hours/phone/website/popularity all come from cache."""
        repo = MagicMock(find=AsyncMock(return_value=[_core("y")]))
        live = _object("y").model_copy(
            update={
                "rating": 3.0,
                "phone": "+1-555",
                "website": "https://y",
                "popularity": 50,
            }
        )
        cache = MagicMock(
            mget=AsyncMock(return_value={"google:y": live}),
            mset=AsyncMock(),
        )
        svc = _make_service(repo=repo, cache=cache)
        result = (await svc.find(PlaceQuery(), limit=5))[0]

        assert result.rating == 3.0
        assert result.phone == "+1-555"
        assert result.website == "https://y"
        assert result.popularity == 50

    async def test_no_cache_entry_yields_bare_object(self) -> None:
        """No cache entry → live fields all None, core fields preserved."""
        repo = MagicMock(find=AsyncMock(return_value=[_core("z")]))
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[]),
        )
        svc = _make_service(repo=repo, cache=cache, client=client)
        result = (await svc.find(PlaceQuery(), limit=5))[0]

        assert result.place_name == "Place z"
        assert result.rating is None
        assert result.hours is None
        assert result.phone is None
        assert result.website is None
        assert result.popularity is None
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
        passed_limit = repo.find.call_args.args[1] if len(
            repo.find.call_args.args
        ) > 1 else kwargs.get("limit")
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
        upsert = MagicMock(upsert_many=AsyncMock(return_value=[]))
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )
        results = await svc.find(PlaceQuery(), limit=10)

        passed = client.get_by_ids.call_args.args[0]
        assert set(passed) == {"google:s", "google:f"}
        upsert.upsert_many.assert_awaited_once()
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
        upsert = MagicMock(upsert_many=AsyncMock(return_value=[]))
        svc = _make_service(
            repo=repo, cache=cache, client=client, upsert_service=upsert
        )
        await svc.find(PlaceQuery(place_name="x"), limit=5)

        client.search.assert_not_awaited()
        client.get_by_ids.assert_not_awaited()
        upsert.upsert_many.assert_not_awaited()
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
        upsert = MagicMock(upsert_many=AsyncMock(return_value=[]))
        svc = _make_service(cache=cache, client=client, upsert_service=upsert)

        result = await svc.get_by_ids(["google:a", "google:b"])

        client.get_by_ids.assert_awaited_once_with(["google:b"])
        upsert.upsert_many.assert_awaited_once()
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
        upsert = MagicMock(upsert_many=AsyncMock(return_value=[]))
        svc = _make_service(cache=cache, client=client, upsert_service=upsert)

        result = await svc.get_by_ids(["google:ghost"])

        client.get_by_ids.assert_awaited_once_with(["google:ghost"])
        upsert.upsert_many.assert_not_awaited()
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
        upsert = MagicMock(upsert_many=AsyncMock(return_value=[]))
        svc = _make_service(cache=cache, client=client, upsert_service=upsert)

        result = await svc.get_by_ids(["google:s"])

        client.get_by_ids.assert_awaited_once_with(["google:s"])
        upsert.upsert_many.assert_awaited_once()
        cache.mset.assert_awaited_once()
        assert "google:s" in result

    async def test_only_misses_passed_to_provider(self) -> None:
        """The provider call carries exactly the missing ids, in order."""
        cached = {"google:b": _object("b"), "google:d": _object("d")}
        cache = MagicMock(mget=AsyncMock(return_value=cached), mset=AsyncMock())
        client = MagicMock(get_by_ids=AsyncMock(return_value=[]))
        upsert = MagicMock(upsert_many=AsyncMock(return_value=[]))
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
        upsert = MagicMock(upsert_many=AsyncMock(return_value=[]))
        svc = _make_service(cache=cache, client=client, upsert_service=upsert)

        result = await svc.get_by_ids(["google:a", "google:b", "google:c"])

        assert set(result) == {"google:a", "google:c"}
        cache.mset.assert_awaited_once()
        msetted = cache.mset.call_args.args[0]
        assert {p.provider_id for p in msetted} == {"google:a", "google:c"}

    async def test_persist_writes_in_single_batch(self) -> None:
        """All fetched results upserted in one upsert_many + one mset."""
        fetched = [_object(p) for p in ("a", "b", "c")]
        cache = MagicMock(mget=AsyncMock(return_value={}), mset=AsyncMock())
        client = MagicMock(get_by_ids=AsyncMock(return_value=fetched))
        upsert = MagicMock(upsert_many=AsyncMock(return_value=[]))
        svc = _make_service(cache=cache, client=client, upsert_service=upsert)

        await svc.get_by_ids(["google:a", "google:b", "google:c"])

        upsert.upsert_many.assert_awaited_once()
        upsert_arg = upsert.upsert_many.call_args.args[0]
        assert len(upsert_arg) == 3
        cache.mset.assert_awaited_once_with(fetched)

    async def test_cache_hit_with_none_values_still_treated_as_hit(self) -> None:
        """Cache returning a PlaceObject with rating=None is still a hit;
        we don't second-guess the cache by re-fetching."""
        partial = _object("a").model_copy(
            update={"rating": None, "hours": None, "phone": None}
        )
        cache = MagicMock(
            mget=AsyncMock(return_value={"google:a": partial}),
            mset=AsyncMock(),
        )
        client = MagicMock(get_by_ids=AsyncMock(return_value=[]))
        svc = _make_service(cache=cache, client=client)

        result = await svc.get_by_ids(["google:a"])

        client.get_by_ids.assert_not_awaited()
        assert result["google:a"].rating is None
