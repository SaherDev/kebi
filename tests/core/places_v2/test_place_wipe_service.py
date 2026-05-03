"""Tests for PlaceWipeService — DB wipe + cache eviction stay in lockstep."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from totoro_ai.core.places_v2.models import (
    LocationContext,
    PlaceCore,
    PlaceObject,
    PlaceQuery,
)
from totoro_ai.core.places_v2.place_wipe_service import (
    DEFAULT_RETENTION_DAYS,
    PlaceWipeService,
)
from totoro_ai.core.places_v2.search_service import PlacesSearchService


def _wiped_core(pid: str | None) -> PlaceCore:
    """Post-wipe core: location and refreshed_at already cleared."""
    return PlaceCore(
        id=pid or "anon",
        provider_id=f"google:{pid}" if pid else None,
        place_name=f"Place {pid or 'anon'}",
        location=None,
        refreshed_at=None,
    )


def _svc(
    *, wiped: list[PlaceCore]
) -> tuple[PlaceWipeService, MagicMock, MagicMock]:
    repo = MagicMock(wipe_stale_locations=AsyncMock(return_value=wiped))
    cache = MagicMock(delete_many=AsyncMock())
    return PlaceWipeService(repo=repo, cache=cache), repo, cache


class TestWipeStaleLocations:
    async def test_default_retention_30_days(self) -> None:
        """Cutoff = now - 30 days when retention_days is left at default."""
        svc, repo, _ = _svc(wiped=[_wiped_core("a"), _wiped_core("b")])

        before = datetime.now(UTC)
        wiped = await svc.wipe_stale_locations()
        after = datetime.now(UTC)

        assert wiped == 2
        repo.wipe_stale_locations.assert_awaited_once()
        cutoff = repo.wipe_stale_locations.call_args.args[0]
        assert before - timedelta(days=30) <= cutoff <= after - timedelta(days=30)

    async def test_custom_retention_window(self) -> None:
        svc, repo, _ = _svc(wiped=[])

        before = datetime.now(UTC)
        await svc.wipe_stale_locations(retention_days=7)
        after = datetime.now(UTC)

        cutoff = repo.wipe_stale_locations.call_args.args[0]
        assert before - timedelta(days=7) <= cutoff <= after - timedelta(days=7)

    async def test_returns_count_of_wiped_rows(self) -> None:
        svc, _, _ = _svc(wiped=[_wiped_core(f"p{i}") for i in range(42)])
        assert await svc.wipe_stale_locations() == 42

    async def test_zero_wiped_skips_cache_delete(self) -> None:
        """No DB rows wiped → no cache call (avoid empty Redis pipeline)."""
        svc, _, cache = _svc(wiped=[])
        assert await svc.wipe_stale_locations() == 0
        cache.delete_many.assert_not_awaited()

    async def test_cache_delete_uses_wiped_provider_ids(self) -> None:
        """Cache eviction targets exactly the provider_ids the DB wiped."""
        svc, _, cache = _svc(
            wiped=[_wiped_core("a"), _wiped_core("c"), _wiped_core("d")]
        )
        await svc.wipe_stale_locations()
        cache.delete_many.assert_awaited_once_with(
            ["google:a", "google:c", "google:d"]
        )

    async def test_skips_cache_for_rows_without_provider_id(self) -> None:
        """A wiped row missing provider_id has no cache key — must be skipped."""
        svc, _, cache = _svc(
            wiped=[_wiped_core("a"), _wiped_core(None), _wiped_core("c")]
        )
        result = await svc.wipe_stale_locations()
        # Count reflects all DB-wiped rows; cache only the namespaced ones.
        assert result == 3
        cache.delete_many.assert_awaited_once_with(["google:a", "google:c"])

    async def test_constant_matches_google_tos_window(self) -> None:
        """Lock the default at 30 to catch accidental changes."""
        assert DEFAULT_RETENTION_DAYS == 30


# ---------------------------------------------------------------------------
# In-memory fakes — small enough to fit in this file; reused across the
# integration tests below to exercise wipe → search → recovery end-to-end
# against real PlaceWipeService and PlacesSearchService instances.
# ---------------------------------------------------------------------------


class _FakeRepo:
    def __init__(self, cores: list[PlaceCore]) -> None:
        self._by_id: dict[str, PlaceCore] = {c.id: c for c in cores if c.id}

    async def find(
        self, query: PlaceQuery, limit: int = 20
    ) -> list[PlaceCore]:
        return list(self._by_id.values())[:limit]

    async def get_by_ids(self, place_ids: list[str]) -> list[PlaceCore]:
        return [self._by_id[p] for p in place_ids if p in self._by_id]

    async def get_by_provider_ids(
        self, provider_ids: list[str]
    ) -> dict[str, PlaceCore]:
        return {
            c.provider_id: c
            for c in self._by_id.values()
            if c.provider_id in provider_ids and c.provider_id is not None
        }

    async def upsert_places(self, cores: list[PlaceCore]) -> list[PlaceCore]:
        out: list[PlaceCore] = []
        for c in cores:
            existing = next(
                (
                    e
                    for e in self._by_id.values()
                    if e.provider_id == c.provider_id
                ),
                None,
            )
            target_id = (existing.id if existing else None) or c.id or "?"
            stored = c.model_copy(update={"id": target_id})
            self._by_id[target_id] = stored
            out.append(stored)
        return out

    async def wipe_stale_locations(
        self, cutoff: datetime
    ) -> list[PlaceCore]:
        wiped: list[PlaceCore] = []
        for cid, c in list(self._by_id.items()):
            if (
                c.location is not None
                and c.refreshed_at is not None
                and c.refreshed_at < cutoff
            ):
                cleared = c.model_copy(
                    update={"location": None, "refreshed_at": None}
                )
                self._by_id[cid] = cleared
                wiped.append(cleared)
        return wiped


class _FakeCache:
    def __init__(
        self, initial: dict[str, PlaceObject] | None = None
    ) -> None:
        self.store: dict[str, PlaceObject] = dict(initial or {})

    async def mget(
        self, provider_ids: list[str]
    ) -> dict[str, PlaceObject]:
        return {p: self.store[p] for p in provider_ids if p in self.store}

    async def mset(
        self,
        places: list[PlaceObject],
        ttl_seconds: int = 0,
    ) -> None:
        for p in places:
            if p.provider_id:
                self.store[p.provider_id] = p

    async def delete_many(self, provider_ids: list[str]) -> None:
        for p in provider_ids:
            self.store.pop(p, None)


class _FakeUpsertService:
    def __init__(self, repo: _FakeRepo) -> None:
        self._repo = repo

    async def upsert_many(
        self, candidates: list[PlaceCore]
    ) -> list[PlaceCore]:
        return await self._repo.upsert_places(candidates)


# ---------------------------------------------------------------------------
# wipe → search integration
# ---------------------------------------------------------------------------


class TestWipeThenSearchRecovery:
    async def test_search_after_wipe_refetches_from_provider(self) -> None:
        """End-to-end: aged row gets wiped (DB + cache), then a search for
        it triggers the by-id provider fallback and repopulates both layers."""
        # ---- Initial state: row + cache entry, both 60 days old. ----
        sixty_days_ago = datetime.now(UTC) - timedelta(days=60)
        warm_core = PlaceCore(
            id="p1",
            provider_id="google:p1",
            place_name="Place p1",
            location=LocationContext(
                lat=13.7, lng=100.5, address="Sukhumvit Soi 11"
            ),
            refreshed_at=sixty_days_ago,
        )
        warm_obj = PlaceObject(
            **warm_core.model_dump(), rating=4.7, popularity=1234
        )
        repo = _FakeRepo([warm_core])
        cache = _FakeCache({"google:p1": warm_obj})

        # ---- Step 1: WIPE. Should clear DB row + drop the cache entry. ----
        wipe_svc = PlaceWipeService(repo=repo, cache=cache)
        wiped_count = await wipe_svc.wipe_stale_locations()

        assert wiped_count == 1
        post_wipe = repo._by_id["p1"]
        assert post_wipe.location is None
        assert post_wipe.refreshed_at is None
        assert "google:p1" not in cache.store

        # ---- Step 2: SEARCH. Stale row + empty cache → provider refetch. ----
        provider_fresh = PlaceObject(
            id="p1",
            provider_id="google:p1",
            place_name="Place p1",
            location=LocationContext(
                lat=13.71, lng=100.51, address="Sukhumvit Soi 11 (refreshed)"
            ),
            rating=4.8,
            popularity=2000,
        )
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[provider_fresh]),
        )
        search_svc = PlacesSearchService(
            repo=repo,
            cache=cache,
            client=client,
            upsert_service=_FakeUpsertService(repo),
        )

        results = await search_svc.find(PlaceQuery(), limit=10)

        # Provider was hit for exactly the wiped id.
        client.get_by_ids.assert_awaited_once_with(["google:p1"])

        # The result reflects the refreshed location and live fields.
        assert len(results) == 1
        out = results[0]
        assert out.location is not None
        assert out.location.lat == 13.71
        assert out.location.address == "Sukhumvit Soi 11 (refreshed)"
        assert out.rating == 4.8
        assert out.popularity == 2000
        # Curated DB field still wins (place_name).
        assert out.place_name == "Place p1"

        # ---- Step 3: persistence — DB and cache are rehydrated. ----
        assert repo._by_id["p1"].location is not None
        assert repo._by_id["p1"].location.lat == 13.71
        assert "google:p1" in cache.store
        assert cache.store["google:p1"].rating == 4.8

    async def test_second_search_after_recovery_is_warm(self) -> None:
        """After the wipe-then-search cycle, a follow-up search must be
        a pure cache hit — no second provider call."""
        sixty_days_ago = datetime.now(UTC) - timedelta(days=60)
        core = PlaceCore(
            id="p2",
            provider_id="google:p2",
            place_name="Place p2",
            location=LocationContext(lat=1.0, lng=1.0),
            refreshed_at=sixty_days_ago,
        )
        repo = _FakeRepo([core])
        cache = _FakeCache(
            {"google:p2": PlaceObject(**core.model_dump(), rating=3.0)}
        )

        await PlaceWipeService(repo=repo, cache=cache).wipe_stale_locations()

        fresh = PlaceObject(
            id="p2",
            provider_id="google:p2",
            place_name="Place p2",
            location=LocationContext(lat=2.0, lng=2.0),
            rating=4.0,
        )
        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[fresh]),
        )
        search_svc = PlacesSearchService(
            repo=repo,
            cache=cache,
            client=client,
            upsert_service=_FakeUpsertService(repo),
        )

        # First search after wipe — provider IS called.
        await search_svc.find(PlaceQuery(), limit=10)
        assert client.get_by_ids.await_count == 1

        # Second search — fully warm now, provider NOT called again.
        results = await search_svc.find(PlaceQuery(), limit=10)
        assert client.get_by_ids.await_count == 1  # unchanged
        assert results[0].rating == 4.0
        assert results[0].location is not None
        assert results[0].location.lat == 2.0

    async def test_wipe_finds_no_aged_rows_search_unaffected(self) -> None:
        """Fresh rows (refreshed today) survive the wipe; subsequent search
        is a pure cache hit and the provider is never called."""
        today = datetime.now(UTC)
        core = PlaceCore(
            id="p3",
            provider_id="google:p3",
            place_name="Place p3",
            location=LocationContext(lat=5.0, lng=5.0),
            refreshed_at=today,
        )
        repo = _FakeRepo([core])
        cache = _FakeCache(
            {"google:p3": PlaceObject(**core.model_dump(), rating=4.5)}
        )

        wiped = await PlaceWipeService(
            repo=repo, cache=cache
        ).wipe_stale_locations()
        assert wiped == 0
        assert "google:p3" in cache.store  # cache untouched
        assert repo._by_id["p3"].location is not None  # DB untouched

        client = MagicMock(
            search=AsyncMock(return_value=[]),
            get_by_ids=AsyncMock(return_value=[]),
        )
        search_svc = PlacesSearchService(
            repo=repo,
            cache=cache,
            client=client,
            upsert_service=_FakeUpsertService(repo),
        )
        results = await search_svc.find(PlaceQuery(), limit=10)

        client.get_by_ids.assert_not_awaited()
        assert results[0].rating == 4.5
        assert results[0].location is not None
        assert results[0].location.lat == 5.0
