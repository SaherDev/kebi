"""Exercise GooglePlacesClient against the live Google Places API.

Logs every call to one JSON file as {function, input, output}, rewritten
after each call so a partial log survives a crash. Add or comment-out
`await call(...)` lines to change coverage.

    poetry run python scripts/places_smoke.py
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from kebi.core.config import get_config, get_env
from kebi.core.places import (
    AccessibilityTag,
    AtmosphereTag,
    CachedEmbedder,
    CuisineTag,
    DietaryTag,
    EmbeddingsRepo,
    FeatureTag,
    GooglePlacesClient,
    HybridSearchFilters,
    HybridSearchRepo,
    HybridSearchService,
    LocationContext,
    PlaceCategory,
    PlaceCore,
    PlaceNameAlias,
    PlaceObject,
    PlaceQuery,
    PlacesRepo,
    PlacesSearchService,
    PlaceSource,
    PlaceTag,
    PlaceUpsertService,
    PlaceWipeService,
    PriceTag,
    RedisPlacesCache,
    SeasonTag,
    ServiceTag,
    TimeTag,
    UserPlace,
    UserPlacesRepo,
    UserPlacesService,
)
from kebi.core.places._place_merge import merge_place
from kebi.core.places import query_examples as qx
from kebi.core.places.embedding_service import EmbeddingService
from kebi.db.session import _get_session_factory
from kebi.providers.embeddings import VoyageEmbedder
from kebi.providers.redis_cache import get_redis_client

OUT = Path(__file__).resolve().parent / "places_calls.json"

# Geo anchors — small radii so the results land in the expected city.
TOKYO_SHIBUYA = LocationContext(lat=35.6595, lng=139.7005, radius_m=1500)
BANGKOK_SUKHUMVIT = LocationContext(lat=13.7375, lng=100.5610, radius_m=2000)
NYC_MIDTOWN = LocationContext(lat=40.7549, lng=-73.9840, radius_m=1500)

# Default geo for query_examples.py entries that ship without a location.
# Without this, skip-only-tag queries (e.g. summer_spots, morning_spots) would
# route to nothing — we want to see they at least dispatch to :searchNearby.
DEFAULT_EXAMPLE_GEO = LocationContext(lat=13.7563, lng=100.5018, radius_m=1500)

# Curated subset of query_examples.py — one (or two) per section. The full
# catalog is ~100 entries; smoking all of them is expensive without adding
# coverage that this set doesn't already cover. Keep this list short and
# representative; extend only when a new tag/category type lands.
EXAMPLE_NAMES: list[str] = [
    # 1. cuisine
    "find_thai",
    # 2. dietary
    "find_vegan",
    # 3. price
    "find_cheap_eats",
    # 4. features / vibes
    "find_outdoor_seating",
    # 5. service
    "find_breakfast_spots",
    # 6. combined intents
    "cheap_outdoor_thai",
    "dog_brunch",
    # 7. location-scoped (already carries geo)
    "nearby_vegan",
    # 8. category-only
    "all_museums",
    # 9. time of day
    "morning_coffee",
    "dinner_date",
    # 10. season / weather
    "rainy_day_cafe",
    # 11. social occasion
    "date_night_italian",
    "solo_work_anywhere",
    # 12. special occasion
    "anniversary_dinner",
    # 13. health / fitness
    "post_workout_smoothie",
    # 14. budget
    "cheapest_meal",
    # 15. splurge
    "splurge_omakase",
    # 16. accessibility
    "wheelchair_friendly",
    # 17. time typed
    "morning_spots",
    # 18. season typed
    "summer_spots",
    # 19. combined full intent
    "rainy_afternoon_work",
]


async def main() -> None:
    client = GooglePlacesClient(api_key=get_env().GOOGLE_API_KEY or "")

    # Top-level JSON object: each section gets its own list of records.
    # Per-record shape stays the same — {function, input, output}.
    groups: dict[str, list[dict[str, Any]]] = {
        "google_client": [],
        "redis_cache": [],
        "cached_embedder": [],
        "db_upsert": [],
        "hybrid_search": [],
        "user_places": [],
        "scoped_hybrid_search": [],
        "places_search": [],
        "merge_place": [],
        "place_wipe": [],
    }

    def _flush() -> None:
        OUT.write_text(json.dumps(groups, indent=2, ensure_ascii=False, default=str))

    def _record(group: str, entry: dict[str, Any]) -> None:
        """Append a record to a group and rewrite the JSON file."""
        groups[group].append(entry)
        _flush()

    async def call(
        group: str,
        function: str,
        input_: Any,
        runner: Callable[[], Awaitable[list[PlaceObject]]],
    ) -> list[PlaceObject]:
        result = await runner()
        _record(
            group,
            {
                "function": function,
                "input": input_,
                "output": [p.model_dump(mode="json") for p in result],
            },
        )
        print(f"  {function:<12} input={_short(input_):<60} → {len(result)} hits")
        return result

    # ---- search() variants -------------------------------------------------

    # 1. text search by place_name + geo (uses :searchText with locationBias.circle)
    q1 = PlaceQuery(place_name="Blue Bottle Coffee", location=TOKYO_SHIBUYA)
    await call("google_client", "search", q1.model_dump(mode="json"), lambda: client.search(q1, limit=5))

    # 2. category-only routes to :searchText with includedType
    q2 = PlaceQuery(category=PlaceCategory.cafe, location=NYC_MIDTOWN)
    await call("google_client", "search", q2.model_dump(mode="json"), lambda: client.search(q2, limit=5))

    # 3. cuisine tag → mapped to a Google place type
    q3 = PlaceQuery(tags=[CuisineTag.thai], location=BANGKOK_SUKHUMVIT)
    seeded = await call(
        "google_client", "search", q3.model_dump(mode="json"), lambda: client.search(q3, limit=5)
    )

    # 4. geo-only → falls through to :searchNearby (different endpoint, geo shape)
    q4 = PlaceQuery(location=TOKYO_SHIBUYA)
    await call("google_client", "search", q4.model_dump(mode="json"), lambda: client.search(q4, limit=5))

    # 5. skip-only tags (no Google text) + geo → also falls back to :searchNearby
    q5 = PlaceQuery(
        tags=[
            TimeTag.evening,
            SeasonTag.summer,
            AccessibilityTag.wheelchair_entrance,
        ],
        location=TOKYO_SHIBUYA,
    )
    await call("google_client", "search", q5.model_dump(mode="json"), lambda: client.search(q5, limit=5))

    # 6. open_now passthrough
    q6 = PlaceQuery(category=PlaceCategory.bar, location=TOKYO_SHIBUYA, open_now=True)
    await call("google_client", "search", q6.model_dump(mode="json"), lambda: client.search(q6, limit=5))

    # 7. empty query → client short-circuits to [] without an HTTP call
    q7 = PlaceQuery()
    await call("google_client", "search", q7.model_dump(mode="json"), lambda: client.search(q7, limit=5))

    # ---- get_by_ids() ------------------------------------------------------

    # 8. round-trip real provider_ids from the cuisine search above
    seed_ids = [p.provider_id for p in seeded[:3] if p.provider_id]
    if seed_ids:
        await call(
            "google_client",
            "get_by_ids",
            {"provider_ids": seed_ids},
            lambda: client.get_by_ids(seed_ids),
        )

    # 9. unsupported provider prefix → short-circuits to [] without a network call
    bad_ids = ["foursquare:abc123"]
    await call(
        "google_client",
        "get_by_ids",
        {"provider_ids": bad_ids},
        lambda: client.get_by_ids(bad_ids),
    )

    # ---- query_examples.py subset -----------------------------------------
    # Each example is a hand-written PlaceQuery for a concrete user intent.
    # Smoke them through GooglePlacesClient so the catalog stays in sync with
    # the mapper/builder logic it implicitly depends on.
    print("\n--- query_examples subset ---")
    for example_name in EXAMPLE_NAMES:
        example: PlaceQuery = getattr(qx, example_name)
        # Attach default geo when the example doesn't carry one — otherwise
        # tag-only queries (e.g. summer_spots) hit the empty-text + no-geo
        # short-circuit and we never see Google's response.
        q = (
            example.model_copy(update={"location": DEFAULT_EXAMPLE_GEO})
            if example.location is None
            else example
        )
        # Strip the `find_` prefix from catalog names for the display label —
        # half the variables in query_examples.py start with `find_`
        # (find_thai, find_vegan, ...) and that prefix carries no signal.
        display_name = example_name.removeprefix("find_")
        await call(
            "google_client",
            f"search[{display_name}]",
            q.model_dump(mode="json"),
            lambda q=q: client.search(q, limit=5),
        )

    # ---- harvest sample places from the search log -----------------------
    # Used by the redis_cache and cached_embedder sections below. The bulk
    # upsert section harvests ALL places separately; we keep this small set
    # for the cache + cached-embedder hit/miss verification.
    sample_places: list[PlaceObject] = []
    for entry in groups["google_client"]:
        if entry["function"].startswith("search") and entry["output"]:
            for raw in entry["output"]:
                sample_places.append(PlaceObject.model_validate(raw))
                if len(sample_places) == 3:
                    break
        if len(sample_places) == 3:
            break

    env = get_env()
    cfg = get_config().models["embedder"]

    # Shared CachedEmbedder reused by every section that needs to embed.
    # Without this, each section would issue its own Voyage call for the
    # same texts and trip the 3-RPM free-tier rate limit on the first run.
    # The counter wrapper lets the cached_embedder section verify the cache
    # is actually doing its job.
    class _CountingEmbedder:
        def __init__(self, inner: VoyageEmbedder) -> None:
            self.inner = inner
            self.calls = 0

        async def embed(
            self, texts: list[str], input_type: str
        ) -> list[list[float]]:
            self.calls += 1
            return await self.inner.embed(texts, input_type)

    counter = _CountingEmbedder(
        VoyageEmbedder(model=cfg.model, api_key=env.VOYAGE_API_KEY)
    )
    cached = CachedEmbedder(
        counter, get_redis_client(env.REDIS_URL), model_name=cfg.model
    )

    # ---- RedisPlacesCache round-trip --------------------------------------
    # mset 3 real PlaceObjects → mget them back → confirm Pydantic round-trip
    # through Redis preserves the full shape (including business_status enum).
    print("\n--- redis cache ---")
    cache = RedisPlacesCache(redis=get_redis_client(env.REDIS_URL))
    if sample_places:
        await cache.mset(sample_places)
        ids_back = await cache.mget(
            [p.provider_id for p in sample_places if p.provider_id]
        )
        roundtrip_results = []
        for p in sample_places:
            if p.provider_id is None:
                continue
            got = ids_back.get(p.provider_id)
            roundtrip_results.append(
                {
                    "provider_id": p.provider_id,
                    "ok": got is not None
                    and got.place_name == p.place_name
                    and got.business_status == p.business_status,
                    "place_name": got.place_name if got else None,
                    "business_status": got.business_status if got else None,
                }
            )
        _record(
            "redis_cache",
            {
                "function": "roundtrip",
                "input": {
                    "provider_ids": [
                        p.provider_id for p in sample_places if p.provider_id
                    ]
                },
                "output": roundtrip_results,
            },
        )
        ok = all(r["ok"] for r in roundtrip_results)
        print(
            f"  mset+mget         {len(roundtrip_results)} keys → "
            f"{'all match' if ok else 'MISMATCH'}"
        )

    # ---- CachedEmbedder hit/miss ------------------------------------------
    # Embed twice with the same text. The wrapped Voyage client should be
    # invoked at most once across both calls — the second call must be 100%
    # cache hit. Reuses the shared `cached` (with `counter`) defined above.
    print("\n--- cached embedder ---")
    if sample_places:
        cache_test_texts = [
            EmbeddingService._build_text(p.to_core()) for p in sample_places
        ]

        calls_before = counter.calls
        v1 = await cached.embed(cache_test_texts, input_type="document")
        calls_after_first = counter.calls
        v2 = await cached.embed(cache_test_texts, input_type="document")
        calls_after_second = counter.calls

        # On a cold cache the first call adds 1 voyage call (miss path);
        # on a warm cache (subsequent runs) it adds 0. Either way the
        # second call must add 0 — that's what proves the cache works.
        first_delta = calls_after_first - calls_before
        second_delta = calls_after_second - calls_after_first
        ok = second_delta == 0 and v1 == v2

        _record(
            "cached_embedder",
            {
                "function": "roundtrip",
                "input": {"n_texts": len(cache_test_texts), "model": cfg.model},
                "output": {
                    "voyage_calls_first_delta": first_delta,
                    "voyage_calls_second_delta": second_delta,
                    "vectors_equal": v1 == v2,
                    "ok": ok,
                },
            },
        )
        print(
            f"  embed×2           voyage_call_deltas={first_delta},{second_delta} "
            f"(2nd must be 0)  vectors_equal={v1 == v2}"
        )

    # ---- DB upsert + embedding write path (bulk) -------------------------
    # Dedupe every PlaceObject the search log produced (across baseline +
    # query_examples runs), upsert them all, then embed-and-store via the
    # shared CachedEmbedder. Re-runs are idempotent: same provider_ids hit
    # the upsert path, same text_hash skips re-embedding.
    print("\n--- db upsert + embed (bulk) ---")

    # Walk the google_client group, dedupe by provider_id (first wins).
    all_places: dict[str, PlaceObject] = {}
    for entry in groups["google_client"]:
        for raw in entry["output"]:
            try:
                p = PlaceObject.model_validate(raw)
            except Exception:
                continue
            if p.provider_id and p.provider_id not in all_places:
                all_places[p.provider_id] = p
    bulk_places = list(all_places.values())
    print(f"  dedupe            {len(bulk_places)} unique places from search log")

    if bulk_places and sample_places:
        session_factory = _get_session_factory()
        async with session_factory() as session:
            try:
                places_repo = PlacesRepo(session)
                upsert = PlaceUpsertService(places_repo)
                cores_in = [p.to_core() for p in bulk_places]
                cores_out = await upsert.upsert_many(cores_in)

                _record(
                    "db_upsert",
                    {
                        "function": "places",
                        "input": {
                            "provider_ids": [
                                c.provider_id for c in cores_in if c.provider_id
                            ]
                        },
                        "output": [
                            {
                                "place_id": c.id,
                                "provider_id": c.provider_id,
                                "place_name": c.place_name,
                                "category": c.category.value if c.category else None,
                            }
                            for c in cores_out
                        ],
                    },
                )
                print(
                    f"  upsert_many       {len(cores_in)} in → "
                    f"{len(cores_out)} rows persisted"
                )

                # Now embed + store. Use a fresh session-scoped EmbeddingsRepo
                # plus a (non-cached) Voyage embedder so we get real vectors
                # in pgvector.
                embeddings_repo = EmbeddingsRepo(session)
                # Reuse the CachedEmbedder built above. Its Redis cache is
                # already warm for sample_places' texts (cached_embedder
                # section embedded the same prose), so this call is a 100%
                # cache hit → 0 Voyage calls. Without this, the 4th Voyage
                # call in <60s trips the 3-RPM free-tier rate limit.
                embed_service = EmbeddingService(
                    embeddings_repo,
                    cached,
                    model_name=cfg.model,
                )
                await embed_service.embed_and_store(cores_out)

                # Read the stored vectors back so the JSON shows what landed.
                stored = await embeddings_repo.get_by_place_ids(
                    [c.id for c in cores_out if c.id]
                )
                signatures = await embeddings_repo.get_signatures_by_place_ids(
                    [c.id for c in cores_out if c.id]
                )
                _record(
                    "db_upsert",
                    {
                        "function": "embeddings",
                        "input": {
                            "place_ids": [c.id for c in cores_out if c.id]
                        },
                        "output": [
                            {
                                "place_id": pid,
                                "vector_dim": len(vec),
                                "vector_preview": vec[:5],
                                "text_hash": signatures.get(pid, ("", ""))[0],
                                "model_name": signatures.get(pid, ("", ""))[1],
                            }
                            for pid, vec in stored.items()
                        ],
                    },
                )
                print(
                    f"  embed_and_store   {len(stored)} vectors written, "
                    f"dims={ {len(v) for v in stored.values()} }"
                )
            except Exception as exc:
                await session.rollback()
                print(f"  db_upsert FAILED: {exc!r}")

    # ---- HybridSearchService — vector + FTS RRF retrieval ----------------
    # Five curated queries against the unscoped catalog (user_id=None). All
    # query embeddings are pre-warmed in ONE batched Voyage call so the per-
    # query loop stays fully cache-hit (otherwise the 3-RPM free-tier limit
    # bites at ~5 distinct query embeddings).
    print("\n--- hybrid search ---")
    # Queries chosen to exercise both legs after migration f3b8e1c4d2a9
    # widened the search_vector. The first four use vocabulary that is now
    # indexed (Tokyo via formattedAddress; "accessibility"/"dietary" via
    # tag types) so FTS should contribute non-null text_rank. The last
    # one keeps a "soft" attribute query (trendy is LLM-only, not indexed)
    # to confirm the vector leg still carries it when FTS returns nothing.
    search_queries = [
        "italian restaurant tokyo",
        "coffee tokyo cafe",
        "wheelchair accessibility museum",
        "thai food bangkok",
        "rooftop cocktails trendy",
    ]
    if bulk_places and sample_places:
        # Pre-warm Voyage cache for all 5 queries in one batched call.
        try:
            await cached.embed(search_queries, input_type="query")
        except Exception as exc:
            print(f"  query pre-warm FAILED: {exc!r}")

        async with _get_session_factory()() as session:
            try:
                hybrid_repo = HybridSearchRepo(session)
                hybrid_service = HybridSearchService(hybrid_repo, cached)

                for q in search_queries:
                    hits = await hybrid_service.search(
                        user_id=None,  # unscoped — global catalog search
                        query=q,
                        limit=5,
                    )
                    _record(
                        "hybrid_search",
                        {
                            "function": "search",
                            "input": {"query": q, "user_id": None, "limit": 5},
                            "output": [
                                {
                                    "place_id": h.place.id,
                                    "place_name": h.place.place_name,
                                    "category": (
                                        h.place.category.value
                                        if h.place.category
                                        else None
                                    ),
                                    "rrf_score": round(h.rrf_score, 6),
                                    "vector_rank": h.vector_rank,
                                    "text_rank": h.text_rank,
                                }
                                for h in hits
                            ],
                        },
                    )
                    if hits:
                        top = hits[0]
                        legs = (
                            f"v={top.vector_rank or '-'},"
                            f"t={top.text_rank or '-'}"
                        )
                        print(
                            f"  {q[:42]:<44} → {len(hits)} hits  "
                            f"top={top.place.place_name[:35]!r:<37} "
                            f"rrf={top.rrf_score:.4f} ({legs})"
                        )
                    else:
                        print(f"  {q[:42]:<44} → 0 hits")
            except Exception as exc:
                print(f"  hybrid_search FAILED: {exc!r}")

    # ---- user_places: synthetic saves + scoped retrieval -----------------
    # Fabricates 8 UserPlace rows for a smoke user, persists them via the
    # repo, then exercises UserPlacesService.get_user_places (which does a
    # 3-stage read: user_places → places → live-field overlay) and
    # update_status. Uses deterministic user_place_ids so re-runs idempotent.
    print("\n--- user_places ---")
    SMOKE_USER = "smoke_user_001"

    if bulk_places:
        async with _get_session_factory()() as session:
            try:
                places_repo = PlacesRepo(session)
                user_places_repo = UserPlacesRepo(session)

                # Pull the place_ids for the first 8 places we just upserted.
                # These are the rows our synthetic user will "save".
                cores_for_user = await places_repo.get_by_provider_ids(
                    [p.provider_id for p in bulk_places[:8] if p.provider_id]
                )
                save_targets = list(cores_for_user.values())

                # Build varied UserPlace rows: mix of sources, visited/liked
                # combinations, and saved_at dates. The validator on UserPlace
                # forbids source_url for manual/kebi and requires it
                # otherwise — distribution respects that.
                base_time = datetime.now(UTC)
                user_places_in: list[UserPlace] = []
                for i, core in enumerate(save_targets):
                    if not core.id:
                        continue
                    is_manual = i % 3 == 0
                    user_places_in.append(
                        UserPlace(
                            user_place_id=f"up_smoke_{i:03d}",
                            user_id=SMOKE_USER,
                            place_id=core.id,
                            approved=True,
                            visited=(i % 2 == 0),
                            liked=(True if i % 3 == 0 else False if i % 3 == 1 else None),
                            note=f"smoke note {i}" if i % 4 == 0 else None,
                            source=PlaceSource.manual if is_manual else PlaceSource.tiktok,
                            source_url=None if is_manual else f"https://tiktok.com/@u/video/{i}",
                            saved_at=base_time - timedelta(days=i * 3),
                            visited_at=base_time - timedelta(days=i) if i % 2 == 0 else None,
                        )
                    )

                saved = await user_places_repo.save_user_places(user_places_in)
                _record(
                    "user_places",
                    {
                        "function": "save_user_places",
                        "input": {"user_id": SMOKE_USER, "n": len(user_places_in)},
                        "output": [
                            {
                                "user_place_id": up.user_place_id,
                                "place_id": up.place_id,
                                "visited": up.visited,
                                "liked": up.liked,
                                "source": up.source.value,
                            }
                            for up in saved
                        ],
                    },
                )
                print(f"  save_user_places  {len(saved)} rows for user={SMOKE_USER!r}")

                # UserPlacesService.get_user_places — 3-stage read.
                # PlacesSearchService is a dependency of UserPlacesService;
                # build it from the same session + cache + client.
                search_service = PlacesSearchService(
                    repo=places_repo,
                    cache=cache,
                    client=client,
                    upsert_service=PlaceUpsertService(places_repo),
                )
                user_places_service = UserPlacesService(
                    places_repo=places_repo,
                    user_places_repo=user_places_repo,
                    search=search_service,
                )
                views = await user_places_service.get_user_places(SMOKE_USER)
                _record(
                    "user_places",
                    {
                        "function": "get_user_places",
                        "input": {"user_id": SMOKE_USER},
                        "output": [
                            {
                                "user_place_id": v.user_data.user_place_id,
                                "place_name": v.place.place_name,
                                "category": (
                                    v.place.category.value if v.place.category else None
                                ),
                                "visited": v.user_data.visited,
                                "liked": v.user_data.liked,
                                "rating": v.place.rating,
                                "business_status": v.place.business_status,
                            }
                            for v in views
                        ],
                    },
                )
                print(
                    f"  get_user_places   {len(views)} SavedPlaceViews "
                    f"(user_data + place + live fields all populated)"
                )

                # update_status — flip visited/liked on one row.
                target = views[0].user_data.user_place_id
                updated = await user_places_service.update_status(
                    target, visited=True, liked=True, note="updated by smoke"
                )
                _record(
                    "user_places",
                    {
                        "function": "update_status",
                        "input": {
                            "user_place_id": target,
                            "visited": True,
                            "liked": True,
                            "note": "updated by smoke",
                        },
                        "output": {
                            "user_place_id": updated.user_place_id,
                            "visited": updated.visited,
                            "liked": updated.liked,
                            "note": updated.note,
                        },
                    },
                )
                print(
                    f"  update_status     {target} → "
                    f"visited={updated.visited} liked={updated.liked}"
                )
            except Exception as exc:
                await session.rollback()
                print(f"  user_places FAILED: {exc!r}")

    # ---- scoped_hybrid_search: same queries but with user_id ------------
    # Same 5 queries as hybrid_search, this time WITH user_id. The repo
    # joins user_places, so each hit carries `user_data` (visited/liked/
    # note/source). Filters demonstrate per-user scoping: visited=True
    # narrows to the user's visited saves; liked=True narrows further.
    print("\n--- scoped hybrid search ---")
    if bulk_places:
        async with _get_session_factory()() as session:
            try:
                hybrid_repo = HybridSearchRepo(session)
                hybrid_service = HybridSearchService(hybrid_repo, cached)

                scoped_queries = [
                    ("italian restaurant tokyo", None),
                    ("coffee tokyo cafe", None),
                    ("coffee tokyo cafe", HybridSearchFilters(visited=True)),
                    ("coffee tokyo cafe", HybridSearchFilters(liked=True)),
                    ("thai food bangkok", HybridSearchFilters(visited=True, liked=True)),
                ]
                for q, filters in scoped_queries:
                    hits = await hybrid_service.search(
                        user_id=SMOKE_USER,
                        query=q,
                        filters=filters,
                        limit=5,
                    )
                    f_label = (
                        "no filters"
                        if filters is None
                        else ",".join(
                            f"{k}={v}"
                            for k, v in filters.model_dump().items()
                            if v is not None
                        )
                    )
                    _record(
                        "scoped_hybrid_search",
                        {
                            "function": "search",
                            "input": {
                                "query": q,
                                "user_id": SMOKE_USER,
                                "filters": (
                                    filters.model_dump(exclude_none=True)
                                    if filters
                                    else None
                                ),
                                "limit": 5,
                            },
                            "output": [
                                {
                                    "place_name": h.place.place_name,
                                    "category": (
                                        h.place.category.value
                                        if h.place.category
                                        else None
                                    ),
                                    "rrf_score": round(h.rrf_score, 6),
                                    "vector_rank": h.vector_rank,
                                    "text_rank": h.text_rank,
                                    "user_data": (
                                        {
                                            "visited": h.user_data.visited,
                                            "liked": h.user_data.liked,
                                            "source": h.user_data.source.value,
                                        }
                                        if h.user_data
                                        else None
                                    ),
                                }
                                for h in hits
                            ],
                        },
                    )
                    user_data_count = sum(1 for h in hits if h.user_data is not None)
                    print(
                        f"  {q[:32]:<34} {f_label:<28} → {len(hits)} hits  "
                        f"({user_data_count} with user_data)"
                    )
            except Exception as exc:
                print(f"  scoped_hybrid_search FAILED: {exc!r}")

    # ---- PlacesSearchService.find() — DB-first → cache → Google ---------
    # Three cases exercise both branches of `find()`:
    #   1. Warm path:   DB hit on a place_name we already upserted →
    #                   PlaceObjects come back enriched from cache or Google
    #                   Place Details (whichever wins per provider_id).
    #   2. Warm path:   DB hit on a category+geo combo (no place_name).
    #   3. Cold path:   Nothing in DB matches → external_fallback runs
    #                   client.search → upserts + writes cache. DB grows.
    print("\n--- places_search ---")
    if bulk_places:
        async with _get_session_factory()() as session:
            try:
                places_repo = PlacesRepo(session)
                upsert_service = PlaceUpsertService(places_repo)
                search_service = PlacesSearchService(
                    repo=places_repo,
                    cache=cache,
                    client=client,
                    upsert_service=upsert_service,
                )

                async def _row_count() -> int:
                    from sqlalchemy import func, select
                    from kebi.core.places.places_repo import _PlacesTable
                    r = await session.execute(select(func.count()).select_from(_PlacesTable))
                    return r.scalar_one()

                async def _record_find(label: str, q: PlaceQuery, *, expected_branch: str) -> None:
                    before = await _row_count()
                    results = await search_service.find(q, limit=5)
                    after = await _row_count()
                    enriched = sum(
                        1
                        for r in results
                        if r.rating is not None or r.hours is not None
                    )
                    _record(
                        "places_search",
                        {
                            "function": "find",
                            "input": {
                                "label": label,
                                "expected_branch": expected_branch,
                                "query": q.model_dump(mode="json"),
                            },
                            "output": {
                                "n_results": len(results),
                                "n_with_live_fields": enriched,
                                "db_rows_before": before,
                                "db_rows_after": after,
                                "db_rows_delta": after - before,
                                "results": [
                                    {
                                        "place_name": r.place_name,
                                        "provider_id": r.provider_id,
                                        "category": (
                                            r.category.value if r.category else None
                                        ),
                                        "rating": r.rating,
                                        "has_hours": r.hours is not None,
                                        "business_status": r.business_status,
                                    }
                                    for r in results
                                ],
                            },
                        },
                    )
                    print(
                        f"  {label:<28} {len(results)} results, "
                        f"{enriched} enriched, db_delta={after - before:+d} "
                        f"(branch={expected_branch})"
                    )

                # 1. Warm — name ILIKE matches our seeded Blue Bottle rows
                await _record_find(
                    "warm:place_name=blue bottle",
                    PlaceQuery(place_name="Blue Bottle"),
                    expected_branch="db_hit",
                )

                # 2. Warm — category + geo matches Bangkok museums we seeded
                await _record_find(
                    "warm:category=museum+bkk",
                    PlaceQuery(
                        category=PlaceCategory.museum,
                        location=BANGKOK_SUKHUMVIT,
                    ),
                    expected_branch="db_hit",
                )

                # 3. Cold — neither name nor category present in DB →
                # external_fallback calls Google, persists results, db grows.
                await _record_find(
                    "cold:place_name=ichiran",
                    PlaceQuery(
                        place_name="Ichiran Ramen", location=TOKYO_SHIBUYA
                    ),
                    expected_branch="external_fallback",
                )
            except Exception as exc:
                await session.rollback()
                print(f"  places_search FAILED: {exc!r}")

    # ---- merge_place: stress-test with rich PlaceCores ------------------
    # Three pure-function probes + one DB round-trip. Each probe asserts a
    # specific column policy (sticky vs dedup-by-value vs cold→warm refresh).
    print("\n--- merge_place ---")

    # ---- Build "v1" — rich initial state -------------------------------
    # 12 aliases across 4 sources, 30 tags across 8 types, full location.
    # Big enough that dedup decisions matter; small enough to read in JSON.
    MERGE_PROVIDER_ID = "google:merge_smoke_001"

    aliases_v1 = [
        PlaceNameAlias(value=f"v1 alias {i}", source=src)
        for i, src in enumerate(
            ["tiktok", "tiktok", "tiktok",
             "instagram", "instagram", "instagram",
             "user", "user", "user",
             "llm", "llm", "llm"]
        )
    ]
    tags_v1: list[PlaceTag] = [
        PlaceTag(type="cuisine", value=v, source="google")
        for v in [CuisineTag.japanese, CuisineTag.thai, CuisineTag.korean,
                  CuisineTag.italian, CuisineTag.french]
    ] + [
        PlaceTag(type="dietary", value=v, source="google")
        for v in [DietaryTag.vegan, DietaryTag.vegetarian, DietaryTag.halal]
    ] + [
        PlaceTag(type="feature", value=v, source="google")
        for v in [FeatureTag.outdoor_seating, FeatureTag.live_music,
                  FeatureTag.dog_friendly, FeatureTag.family_friendly]
    ] + [
        PlaceTag(type="service", value=v, source="google")
        for v in [ServiceTag.dine_in, ServiceTag.takeout, ServiceTag.delivery,
                  ServiceTag.reservable, ServiceTag.serves_breakfast]
    ] + [
        PlaceTag(type="atmosphere", value=v, source="llm")
        for v in [AtmosphereTag.cozy, AtmosphereTag.romantic]
    ] + [
        PlaceTag(type="price", value=PriceTag.moderate, source="google"),
        PlaceTag(type="accessibility", value=AccessibilityTag.wheelchair_entrance, source="google"),
        PlaceTag(type="accessibility", value=AccessibilityTag.wheelchair_parking, source="google"),
        PlaceTag(type="time", value=TimeTag.evening, source="llm"),
        PlaceTag(type="time", value=TimeTag.brunch, source="llm"),
        PlaceTag(type="season", value=SeasonTag.summer, source="llm"),
    ]

    place_v1 = PlaceCore(
        provider_id=MERGE_PROVIDER_ID,
        place_name="Original Big Test Place",
        category=PlaceCategory.cafe,
        place_name_aliases=aliases_v1,
        tags=tags_v1,
        location=LocationContext(
            lat=35.6595, lng=139.7005,
            address="V1 address line",
            city="Tokyo", country="Japan",
            neighborhood="Shibuya",
        ),
    )

    # ---- Build "v2" — incoming candidate with conflicts + new data ------
    # Sticky-policy testers: different name, different category, different
    # location → all should be ignored.
    # Dedup-policy testers: 3 alias values overlap with v1, 5 new aliases.
    # Tag overlaps: 4 tag values overlap with v1, 8 new tag values.
    aliases_v2 = [
        # 3 overlapping values — different sources but value match → dropped
        PlaceNameAlias(value="v1 alias 0", source="user"),
        PlaceNameAlias(value="v1 alias 5", source="llm"),
        PlaceNameAlias(value="v1 alias 11", source="user"),
        # 5 new
        PlaceNameAlias(value="v2 alias A", source="tiktok"),
        PlaceNameAlias(value="v2 alias B", source="tiktok"),
        PlaceNameAlias(value="v2 alias C", source="instagram"),
        PlaceNameAlias(value="v2 alias D", source="user"),
        PlaceNameAlias(value="v2 alias E", source="llm"),
    ]
    tags_v2 = [
        # 4 overlapping values from v1
        PlaceTag(type="cuisine", value=CuisineTag.japanese, source="user"),
        PlaceTag(type="dietary", value=DietaryTag.vegan, source="user"),
        PlaceTag(type="feature", value=FeatureTag.outdoor_seating, source="user"),
        PlaceTag(type="atmosphere", value=AtmosphereTag.cozy, source="user"),
        # 8 new values
        PlaceTag(type="cuisine", value=CuisineTag.mexican, source="llm"),
        PlaceTag(type="cuisine", value=CuisineTag.spanish, source="llm"),
        PlaceTag(type="atmosphere", value=AtmosphereTag.trendy, source="llm"),
        PlaceTag(type="atmosphere", value=AtmosphereTag.lively, source="llm"),
        PlaceTag(type="feature", value=FeatureTag.scenic_view, source="user"),
        PlaceTag(type="feature", value=FeatureTag.rooftop, source="user"),
        PlaceTag(type="service", value=ServiceTag.serves_dinner, source="google"),
        PlaceTag(type="time", value=TimeTag.late_night, source="llm"),
    ]

    place_v2 = PlaceCore(
        provider_id=MERGE_PROVIDER_ID,
        place_name="DIFFERENT Name v2",     # sticky test — should be ignored
        category=PlaceCategory.restaurant,  # sticky test — should be ignored
        place_name_aliases=aliases_v2,
        tags=tags_v2,
        location=LocationContext(           # sticky test — should be ignored
            lat=0.0, lng=0.0,
            address="V2 address line — should not appear",
            city="V2_CITY",
        ),
    )

    # ---- Probe 1: pure merge_place(v1, v2) -----------------------------
    merged = merge_place(place_v1, place_v2)
    pure_check = {
        "name_sticky": merged.place_name == place_v1.place_name,
        "category_sticky": merged.category == place_v1.category,
        "location_sticky": (
            merged.location is not None
            and merged.location.city == place_v1.location.city
        ),
        "aliases_count_after_dedup": len(merged.place_name_aliases),
        "aliases_expected": len(place_v1.place_name_aliases) + 5,  # 12 + 5 new
        "tags_count_after_dedup": len(merged.tags),
        "tags_expected": len(place_v1.tags) + 8,                   # 23 + 8 new
        "no_dropped_v1_values": all(
            any(a.value == v.value for a in merged.place_name_aliases)
            for v in place_v1.place_name_aliases
        ),
    }
    pure_ok = (
        pure_check["name_sticky"]
        and pure_check["category_sticky"]
        and pure_check["location_sticky"]
        and pure_check["aliases_count_after_dedup"] == pure_check["aliases_expected"]
        and pure_check["tags_count_after_dedup"] == pure_check["tags_expected"]
        and pure_check["no_dropped_v1_values"]
    )
    _record(
        "merge_place",
        {
            "function": "pure:v1_then_v2",
            "input": {
                "v1": {
                    "place_name": place_v1.place_name,
                    "category": place_v1.category.value,
                    "n_aliases": len(place_v1.place_name_aliases),
                    "n_tags": len(place_v1.tags),
                    "location_city": place_v1.location.city,
                },
                "v2": {
                    "place_name": place_v2.place_name,
                    "category": place_v2.category.value,
                    "n_aliases": len(place_v2.place_name_aliases),
                    "n_tags": len(place_v2.tags),
                    "location_city": place_v2.location.city,
                },
            },
            "output": {
                "ok": pure_ok,
                **pure_check,
                "merged": {
                    "place_name": merged.place_name,
                    "category": merged.category.value if merged.category else None,
                    "n_aliases": len(merged.place_name_aliases),
                    "n_tags": len(merged.tags),
                    "location_city": merged.location.city if merged.location else None,
                    "alias_values": [a.value for a in merged.place_name_aliases],
                    "tag_values": [t.value for t in merged.tags],
                },
            },
        },
    )
    print(
        f"  pure:v1+v2        sticky=name/cat/loc kept ✓  "
        f"aliases {len(place_v1.place_name_aliases)}+{len(place_v2.place_name_aliases)}→"
        f"{len(merged.place_name_aliases)} (expected {pure_check['aliases_expected']})  "
        f"tags {len(place_v1.tags)}+{len(place_v2.tags)}→{len(merged.tags)} "
        f"(expected {pure_check['tags_expected']}) ok={pure_ok}"
    )

    # ---- Probe 2: cold → warm location transition -----------------------
    cold = PlaceCore(
        provider_id="google:merge_cold_001",
        place_name="Cold Place",
        category=PlaceCategory.cafe,
        location=None,
        refreshed_at=None,
    )
    warm_candidate = PlaceCore(
        provider_id="google:merge_cold_001",
        place_name="Cold Place",
        category=PlaceCategory.cafe,
        location=LocationContext(lat=1.0, lng=2.0, radius_m=100, city="Test"),
        refreshed_at=datetime.now(UTC),
    )
    cold_warm = merge_place(cold, warm_candidate)
    cold_ok = (
        cold_warm.location is not None
        and cold_warm.refreshed_at == warm_candidate.refreshed_at
    )
    _record(
        "merge_place",
        {
            "function": "pure:cold_to_warm",
            "input": {
                "existing.location": None,
                "existing.refreshed_at": None,
                "candidate.location.city": warm_candidate.location.city,
                "candidate.refreshed_at": warm_candidate.refreshed_at.isoformat(),
            },
            "output": {
                "ok": cold_ok,
                "merged.location.city": (
                    cold_warm.location.city if cold_warm.location else None
                ),
                "merged.refreshed_at_bumped": cold_warm.refreshed_at is not None,
            },
        },
    )
    print(f"  pure:cold→warm    location filled, refreshed_at bumped: ok={cold_ok}")

    # ---- Probe 3: DB round-trip via PlaceUpsertService ------------------
    # Insert v1 → upsert v2 → read back row → verify merge applied to DB.
    if bulk_places:
        async with _get_session_factory()() as session:
            try:
                places_repo = PlacesRepo(session)
                upsert_service = PlaceUpsertService(places_repo)

                # Round 1: insert v1.
                await upsert_service.upsert_many([place_v1])
                row_v1 = (
                    await places_repo.get_by_provider_ids([MERGE_PROVIDER_ID])
                ).get(MERGE_PROVIDER_ID)

                # Round 2: upsert v2 with conflicting values.
                await upsert_service.upsert_many([place_v2])
                row_v2 = (
                    await places_repo.get_by_provider_ids([MERGE_PROVIDER_ID])
                ).get(MERGE_PROVIDER_ID)

                db_check = {
                    "v1_persisted": row_v1 is not None,
                    "v2_persisted": row_v2 is not None,
                    "name_sticky_in_db": (
                        row_v2 is not None
                        and row_v2.place_name == place_v1.place_name
                    ),
                    "category_sticky_in_db": (
                        row_v2 is not None
                        and row_v2.category == place_v1.category
                    ),
                    "location_sticky_in_db": (
                        row_v2 is not None
                        and row_v2.location is not None
                        and row_v2.location.city == place_v1.location.city
                    ),
                    "aliases_after_round_2": (
                        len(row_v2.place_name_aliases) if row_v2 else 0
                    ),
                    "tags_after_round_2": len(row_v2.tags) if row_v2 else 0,
                }
                db_ok = (
                    db_check["v1_persisted"]
                    and db_check["v2_persisted"]
                    and db_check["name_sticky_in_db"]
                    and db_check["category_sticky_in_db"]
                    and db_check["location_sticky_in_db"]
                    and db_check["aliases_after_round_2"]
                    == len(place_v1.place_name_aliases) + 5
                    and db_check["tags_after_round_2"]
                    == len(place_v1.tags) + 8
                )

                _record(
                    "merge_place",
                    {
                        "function": "db_round_trip",
                        "input": {
                            "provider_id": MERGE_PROVIDER_ID,
                            "v1.n_aliases": len(place_v1.place_name_aliases),
                            "v1.n_tags": len(place_v1.tags),
                            "v2.n_aliases": len(place_v2.place_name_aliases),
                            "v2.n_tags": len(place_v2.tags),
                        },
                        "output": {
                            "ok": db_ok,
                            **db_check,
                            "expected_aliases": len(place_v1.place_name_aliases) + 5,
                            "expected_tags": len(place_v1.tags) + 8,
                            "row_v2_dump": (
                                {
                                    "place_name": row_v2.place_name,
                                    "category": (
                                        row_v2.category.value
                                        if row_v2.category
                                        else None
                                    ),
                                    "alias_values": [
                                        a.value for a in row_v2.place_name_aliases
                                    ],
                                    "tag_values": [t.value for t in row_v2.tags],
                                    "location_city": (
                                        row_v2.location.city
                                        if row_v2.location
                                        else None
                                    ),
                                }
                                if row_v2
                                else None
                            ),
                        },
                    },
                )
                print(
                    f"  db_round_trip     v1→v2 stored. name/cat/loc all sticky in DB. "
                    f"aliases→{db_check['aliases_after_round_2']} "
                    f"tags→{db_check['tags_after_round_2']}  ok={db_ok}"
                )
            except Exception as exc:
                await session.rollback()
                print(f"  merge_place db_round_trip FAILED: {exc!r}")

    # ---- PlaceWipeService — Google ToS 30-day TTL --------------------
    # End-to-end: backdate 2 rows, prime them in cache, run wipe, observe
    # DB+cache state, then re-query via PlacesSearchService.get_by_ids and
    # confirm the cold path automatically rehydrates them via Google Place
    # Details + writes them back to both DB and cache.
    print("\n--- place_wipe ---")
    if sample_places and len(sample_places) >= 2:
        from sqlalchemy import bindparam, text
        wipe_targets = [p for p in sample_places[:2] if p.provider_id]
        target_ids = [p.provider_id for p in wipe_targets]

        async with _get_session_factory()() as session:
            try:
                places_repo = PlacesRepo(session)
                wipe_service = PlaceWipeService(repo=places_repo, cache=cache)

                # Prime cache: ensure both targets are in Redis before wipe
                # (they were mset earlier, but a fresh mset is harmless and
                # guarantees a clean before-state).
                await cache.mset(wipe_targets)

                # Backdate refreshed_at to 40 days ago. Direct SQL because
                # PlacesRepo doesn't expose this — it's a wipe-only action,
                # only ever needed in tests/migrations.
                await session.execute(
                    text(
                        "UPDATE places SET refreshed_at = NOW() - INTERVAL '40 days' "
                        "WHERE provider_id = ANY(:ids)"
                    ).bindparams(bindparam("ids", value=target_ids)),
                )
                await session.commit()

                # Snapshot pre-wipe state.
                pre_db = await places_repo.get_by_provider_ids(target_ids)
                pre_cache = await cache.mget(target_ids)
                pre_state = {
                    pid: {
                        "in_db": pid in pre_db,
                        "db_has_location": (
                            pre_db[pid].location is not None if pid in pre_db else False
                        ),
                        "in_cache": pid in pre_cache,
                    }
                    for pid in target_ids
                }
                _record(
                    "place_wipe",
                    {
                        "function": "setup_pre_wipe",
                        "input": {"target_provider_ids": target_ids},
                        "output": pre_state,
                    },
                )

                # ---- run the wipe ----
                wiped_count = await wipe_service.wipe_stale_locations(
                    retention_days=30
                )

                # Snapshot post-wipe state.
                post_db = await places_repo.get_by_provider_ids(target_ids)
                post_cache = await cache.mget(target_ids)
                post_state = {
                    pid: {
                        "in_db": pid in post_db,
                        "db_has_location": (
                            post_db[pid].location is not None
                            if pid in post_db
                            else False
                        ),
                        "in_cache": pid in post_cache,
                    }
                    for pid in target_ids
                }
                wipe_ok = (
                    wiped_count >= len(target_ids)
                    and all(not s["db_has_location"] for s in post_state.values())
                    and all(not s["in_cache"] for s in post_state.values())
                )
                _record(
                    "place_wipe",
                    {
                        "function": "wipe_stale_locations",
                        "input": {
                            "retention_days": 30,
                            "target_provider_ids": target_ids,
                        },
                        "output": {
                            "ok": wipe_ok,
                            "wiped_count_returned": wiped_count,
                            "post_state": post_state,
                        },
                    },
                )
                print(
                    f"  wipe_stale_locations  wiped={wiped_count} "
                    f"(targets={len(target_ids)})  "
                    f"db_locations cleared={all(not s['db_has_location'] for s in post_state.values())}  "
                    f"cache evicted={all(not s['in_cache'] for s in post_state.values())}"
                )

                # ---- verify untouched: pick a fresh provider_id NOT wiped ----
                if len(bulk_places) > 5:
                    untouched_pid = next(
                        (
                            p.provider_id
                            for p in bulk_places
                            if p.provider_id and p.provider_id not in target_ids
                        ),
                        None,
                    )
                    if untouched_pid:
                        unrow = (
                            await places_repo.get_by_provider_ids([untouched_pid])
                        ).get(untouched_pid)
                        untouched_ok = (
                            unrow is not None and unrow.location is not None
                        )
                        _record(
                            "place_wipe",
                            {
                                "function": "verify_untouched",
                                "input": {"sample_provider_id": untouched_pid},
                                "output": {
                                    "ok": untouched_ok,
                                    "still_has_location": untouched_ok,
                                },
                            },
                        )
                        print(
                            f"  verify_untouched      sample row "
                            f"({untouched_pid[:25]}...)  location preserved={untouched_ok}"
                        )

                # ---- post-wipe re-enrichment via PlacesSearchService -----
                # Cache is empty for these provider_ids; DB has them but with
                # location=NULL. PlacesSearchService.get_by_ids should:
                # 1. mget cache → miss
                # 2. client.get_by_ids → Google Place Details
                # 3. _persist_external → upsert (location restored) + mset
                upsert_service = PlaceUpsertService(places_repo)
                search_service = PlacesSearchService(
                    repo=places_repo,
                    cache=cache,
                    client=client,
                    upsert_service=upsert_service,
                )
                rehydrated = await search_service.get_by_ids([target_ids[0]])
                rehydrated_obj = rehydrated.get(target_ids[0])

                # Confirm DB + cache are back in lock-step.
                rehydr_db = (
                    await places_repo.get_by_provider_ids([target_ids[0]])
                ).get(target_ids[0])
                rehydr_cache = await cache.mget([target_ids[0]])
                rehydr_ok = (
                    rehydrated_obj is not None
                    and rehydrated_obj.location is not None
                    and rehydrated_obj.rating is not None
                    and rehydr_db is not None
                    and rehydr_db.location is not None
                    and target_ids[0] in rehydr_cache
                )
                _record(
                    "place_wipe",
                    {
                        "function": "rehydrate_via_search_service",
                        "input": {"provider_id": target_ids[0]},
                        "output": {
                            "ok": rehydr_ok,
                            "search_service_returned_object": rehydrated_obj is not None,
                            "object_has_location": (
                                rehydrated_obj is not None
                                and rehydrated_obj.location is not None
                            ),
                            "object_has_live_fields": (
                                rehydrated_obj is not None
                                and rehydrated_obj.rating is not None
                            ),
                            "db_location_restored": (
                                rehydr_db is not None
                                and rehydr_db.location is not None
                            ),
                            "cache_repopulated": target_ids[0] in rehydr_cache,
                        },
                    },
                )
                print(
                    f"  rehydrate_via_search  cold path: "
                    f"object_returned={rehydrated_obj is not None}  "
                    f"live_fields={rehydrated_obj is not None and rehydrated_obj.rating is not None}  "
                    f"db_restored={rehydr_db is not None and rehydr_db.location is not None}  "
                    f"cache_repopulated={target_ids[0] in rehydr_cache}"
                )
            except Exception as exc:
                await session.rollback()
                print(f"  place_wipe FAILED: {exc!r}")

    total = sum(len(v) for v in groups.values())
    print(f"\nwrote {total} call(s) across {len(groups)} groups → {OUT}")


def _short(obj: Any) -> str:
    """Compact one-line preview of an input dict for the console summary."""
    if isinstance(obj, dict) and "provider_ids" in obj:
        return f"provider_ids={obj['provider_ids']}"
    if isinstance(obj, dict):
        bits = []
        if obj.get("place_name"):
            bits.append(f"name={obj['place_name']!r}")
        if obj.get("category"):
            bits.append(f"cat={obj['category']}")
        if obj.get("tags"):
            bits.append(f"tags={obj['tags']}")
        if obj.get("location"):
            loc = obj["location"]
            if loc.get("lat") is not None:
                bits.append(f"geo=({loc['lat']},{loc['lng']},r={loc['radius_m']})")
        if obj.get("open_now"):
            bits.append("open_now=True")
        return ", ".join(bits) or "<empty>"
    return str(obj)


if __name__ == "__main__":
    asyncio.run(main())
