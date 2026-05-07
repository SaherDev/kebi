# Quickstart — Verify `019-places-service` Locally

**Feature**: 019-places-service
**Audience**: anyone (you, future-you, a follow-up agent) verifying that the new data layer is wired correctly after `/speckit.tasks` and `/speckit.implement` complete.

This is **not** a tutorial for using the data layer in production. It is the minimum set of commands to prove that the feature works end-to-end on a fresh checkout. The follow-up features that wire the save/recall/consult tools will provide a real end-user quickstart.

---

## Prerequisites

- The `019-places-service` branch is checked out (`git branch --show-current` should print `019-places-service`).
- Docker Desktop is running.
- `poetry` is installed and available on PATH.
- A populated `.env` file at the repo root (already a symlink to `kebi-config/secrets/ai.env.local` per ADR-051). At minimum the following keys must be present and non-empty: `DATABASE_URL`, `REDIS_URL`, `GOOGLE_API_KEY`. Other keys can be empty for this verification.

---

## Step 1 — Install and start services

```bash
poetry install
docker compose up -d              ; starts PostgreSQL and Redis
```

Wait until both containers are healthy:

```bash
docker compose ps
```

---

## Step 2 — Run the relocation seed script (only if the database has legacy data)

If the `places` table already has rows (e.g. you have run extraction on this database before), run the seed script **before** applying the Alembic migration. The migration will not crash if you skip this step on an empty database, but it will lose the legacy `cuisine`, `price_range`, and `lat`/`lng`/`address` data on a populated one.

```bash
poetry run python scripts/seed_migration.py
```

Expected output on an empty database:

```
seed_migration: scanned 0 rows
  cuisine relocated:        0
  price_range mapped:       0  (unmapped: 0)
  ambiance relocated:       0
  geo cache seeded:         0
  geo data lost (no pid):   0
done.
```

On a populated database, the counts reflect what was relocated. Anything in the `geo data lost` line is logged with row IDs to `scripts/seed_migration.log`.

---

## Step 3 — Apply the Alembic migration

```bash
poetry run alembic upgrade head
```

Expected: a new revision named `places_service_schema` runs and prints `Running upgrade ... -> ..., places_service_schema`.

Verify the schema change landed:

```bash
poetry run python -c "
from sqlalchemy import inspect
from kebi.db.session import engine
import asyncio

async def main():
    async with engine.begin() as conn:
        cols = await conn.run_sync(lambda c: [col['name'] for col in inspect(c).get_columns('places')])
        print('places columns:', sorted(cols))

asyncio.run(main())
"
```

The output should include the new columns: `place_type`, `subcategory`, `tags`, `attributes`, `provider_id`. The legacy columns (`address`, `cuisine`, `price_range`, `lat`, `lng`, `external_provider`, `external_id`, `confidence`, `validated_at`, `ambiance`) are still present (revision A tombstones them; revision B in a follow-up feature drops them).

Verify the partial unique index exists:

```bash
docker compose exec -T postgres psql -U postgres -d kebi -c "\\d+ places" | grep uq_places_provider_id
```

Expected: a line containing `uq_places_provider_id` and `WHERE provider_id IS NOT NULL`.

---

## Step 4 — Run the unit tests

```bash
poetry run pytest tests/core/places/ -v
```

All tests must pass. Expected count: roughly 25-35 tests across the five new test files. (The exact count comes from `/speckit.tasks`.)

If any test fails, do **not** continue. Read the failure, fix the implementation, and re-run.

---

## Step 5 — Type-check and lint the new module

```bash
poetry run mypy src/kebi/core/places/
poetry run ruff check src/kebi/core/places/ tests/core/places/
```

Both must exit 0. `mypy --strict` is the bar (constitution IX).

---

## Step 6 — Smoke-test the service via a Python REPL

This exercises the full data layer with an in-memory mock of the provider client. It does NOT call Google.

```bash
poetry run python <<'PY'
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from kebi.core.places import (
    PlacesService,
    PlaceCreate,
    PlaceObject,
    PlaceType,
    PlaceProvider,
    PlaceSource,
    PlaceAttributes,
    DuplicatePlaceError,
)
from kebi.core.places.repository import PlacesRepository
from kebi.core.places.cache import PlacesCache
from kebi.db.session import async_session
from kebi.providers.redis_cache import get_redis

async def main():
    redis = await get_redis()
    async with async_session() as session:
        repo = PlacesRepository(session)
        cache = PlacesCache(redis)

        # Mock provider — no real Google calls
        client = AsyncMock()
        client.get_place_details = AsyncMock(return_value={
            "lat": 13.7563, "lng": 100.5018, "address": "Bangkok",
            "hours": {"timezone": "Asia/Bangkok", "monday": "09:00-22:00"},
            "rating": 4.5, "phone": "+66...", "photo_url": None, "popularity": 0.7,
        })

        svc = PlacesService(repo, cache, client)

        # 1) create
        created = await svc.create(PlaceCreate(
            user_id="quickstart-user",
            place_name="Quickstart Cafe",
            place_type=PlaceType.food_and_drink,
            subcategory="cafe",
            tags=["hidden-gem"],
            attributes=PlaceAttributes(cuisine="thai", price_hint="moderate"),
            source=PlaceSource.manual,
            external_id="ChIJ_quickstart_test_id",
            provider=PlaceProvider.google,
        ))
        print("created:", created.place_id, created.provider_id)
        assert created.geo_fresh is False
        assert created.enriched is False

        # 2) duplicate detection
        try:
            await svc.create(PlaceCreate(
                user_id="quickstart-user",
                place_name="Quickstart Cafe (dupe)",
                place_type=PlaceType.food_and_drink,
                external_id="ChIJ_quickstart_test_id",
                provider=PlaceProvider.google,
            ))
            print("FAIL: duplicate was not detected")
        except DuplicatePlaceError as e:
            print("duplicate detected:", [c.provider_id for c in e.conflicts])
            await session.rollback()

        # 3) get
        fetched = await svc.get(created.place_id)
        print("fetched:", fetched.place_name if fetched else None)

        # 4) enrich (consult mode — full)
        enriched = await svc.enrich_batch([created], geo_only=False)
        print("enriched lat/lng:", enriched[0].lat, enriched[0].lng)
        print("enriched flags:", enriched[0].geo_fresh, enriched[0].enriched)
        assert enriched[0].geo_fresh is True
        assert enriched[0].enriched is True

        # 5) recall mode — geo only, should NOT have called provider this time
        client.get_place_details.reset_mock()
        recalled = await svc.enrich_batch([created], geo_only=True)
        print("recalled flags:", recalled[0].geo_fresh, recalled[0].enriched)
        assert recalled[0].enriched is False
        assert client.get_place_details.call_count == 0
        print("recall path made zero provider calls — correct")

        # cleanup
        await session.rollback()

    await redis.aclose()
    print("quickstart OK")

asyncio.run(main())
PY
```

Expected console output (counts and IDs will differ):

```
created: 8b3f...  google:ChIJ_quickstart_test_id
duplicate detected: ['google:ChIJ_quickstart_test_id']
fetched: Quickstart Cafe
enriched lat/lng: 13.7563 100.5018
enriched flags: True True
recalled flags: True False
recall path made zero provider calls — correct
quickstart OK
```

If any line says `FAIL` or any assertion raises, the data layer is misbehaving — read the failure and fix.

---

## Step 7 — Verify cache keys exist in Redis

```bash
docker compose exec -T redis redis-cli KEYS 'places:*'
```

After step 6 you should see at least:

- `places:geo:google:ChIJ_quickstart_test_id`
- `places:enrichment:google:ChIJ_quickstart_test_id`

Inspect the geo cache value:

```bash
docker compose exec -T redis redis-cli GET 'places:geo:google:ChIJ_quickstart_test_id'
```

Expected: a JSON string with `lat`, `lng`, `address`, `cached_at`. Inspect the TTL:

```bash
docker compose exec -T redis redis-cli TTL 'places:geo:google:ChIJ_quickstart_test_id'
```

Expected: a value close to 2_592_000 (30 days in seconds).

---

## Step 8 — Tear down

```bash
docker compose down              ; stop services, keep volume
```

To wipe the database too:

```bash
docker compose down -v           ; stop services and remove volumes
```

---

## What this quickstart does NOT cover

- Wiring `PlacesService` into a FastAPI route. There is no route in this feature. The follow-up features that build the save/recall/consult tools will provide route-level smoke tests.
- ExtractionService migration. ExtractionService still uses the legacy `SQLAlchemyPlaceRepository` after this feature ships. The follow-up feature `020-…` (or similar) handles that migration and drops the legacy columns.
- Real Google Places API calls. The smoke test in step 6 mocks the provider client. Calling Google is exercised by integration tests in the follow-up feature.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `alembic upgrade head` fails with "column already exists" | A previous attempt of this migration partially ran | `alembic downgrade -1` then re-run `upgrade head` |
| Step 6 fails with `RuntimeError: Failed to save place` | The legacy `address` column is still NOT NULL | Confirm revision A landed (`alembic current`); re-apply if needed |
| `KEYS places:*` returns nothing after step 6 | Redis client is connecting to the wrong instance | Check `REDIS_URL` in `.env`; confirm `docker compose ps` shows `redis` running |
| `mypy --strict` fails on `PlaceObject.attributes` | `PlaceAttributes()` default mutable issue | Use `Field(default_factory=PlaceAttributes)`, not bare `= PlaceAttributes()` |
| `pytest` fails with `RuntimeError: There is no current event loop` | `asyncio_mode = "auto"` not set in `pyproject.toml` | This is already configured in the repo; check if a local override broke it |
