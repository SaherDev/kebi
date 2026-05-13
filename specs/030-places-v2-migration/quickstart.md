# Quickstart — Verify the Extraction → v2 Cutover Locally

End-to-end smoke for a developer reviewing this feature on their machine. Assumes the repo is checked out on branch `030-places-v2-migration` and Docker is available.

## 1. Install + boot

```bash
poetry install
docker compose up -d                              # starts Postgres + Redis
poetry run alembic upgrade head                   # ensures places_v2/user_places/place_embeddings_v2 exist
poetry run uvicorn kebi.api.main:app --reload
```

## 2. Confirm no legacy imports in extraction

```bash
grep -rn "from kebi.core.places import\|from kebi.core.places\." src/kebi/core/extraction/ src/kebi/api/routes/extraction.py src/kebi/api/schemas/extract_place.py src/kebi/core/agent/tools/save_tool.py
```

**Expected**: zero results (FR-008, SC-001).

## 3. Confirm the searcher is gone

```bash
test ! -f src/kebi/core/extraction/searcher.py && echo "deleted ✓"
grep -rn "PlacesSearcher\|SearchMatch" src/ tests/
```

**Expected**: file does not exist; grep returns zero results (ADR-070).

## 4. Run the extraction tests

```bash
poetry run pytest tests/core/extraction/ -v
```

**Expected**: all green. `test_v2_cutover_parity.py` exercises partition counts, latency p95, and response shape against committed baselines (research.md R-09).

## 5. Run a real extraction end-to-end

In one terminal:

```bash
curl -X POST http://localhost:8000/v1/extract \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "test-user",
    "raw_input": "https://www.instagram.com/p/CABCDEFGHIJ/",
    "supplementary_text": ""
  }'
```

(Substitute any URL the extraction pipeline supports.)

## 6. Inspect what landed where

```bash
# Postgres — v2 table got the new row
docker compose exec postgres psql -U kebi -c "SELECT id, provider_id, place_name, categories, jsonb_array_length(tags::jsonb) AS tag_count FROM places_v2 ORDER BY created_at DESC LIMIT 5;"

# Postgres — legacy table did NOT (extraction-side)
docker compose exec postgres psql -U kebi -c "SELECT count(*) FROM places WHERE created_at > now() - interval '5 minutes';"
# expected: 0

# Postgres — embedding landed in v2
docker compose exec postgres psql -U kebi -c "SELECT place_id, model_name FROM place_embeddings_v2 ORDER BY created_at DESC LIMIT 5;"

# Redis — v2 cache populated
docker compose exec redis redis-cli KEYS "*" | head -20
# Should see v2 cache keys (single flat namespace), NOT places:geo:*, places:enrichment:*, places:geocode:*

# Redis — legacy namespaces did not receive new writes
docker compose exec redis redis-cli KEYS "places:geo:*" | wc -l
docker compose exec redis redis-cli KEYS "places:enrichment:*" | wc -l
docker compose exec redis redis-cli KEYS "places:geocode:*" | wc -l
# Each should be the same count as before this extraction (no new entries)
```

**Expected**: new row in `places_v2`, zero new rows in legacy `places`, new row in `place_embeddings_v2`, cache populated under v2 namespace only (FR-001/002/003, SC-002/003/004).

## 7. Confirm response shape

```bash
curl -X POST http://localhost:8000/v1/extract \
  -H "Content-Type: application/json" \
  -d '{"user_id": "test-user", "raw_input": "Joe & The Juice on Sukhumvit"}' \
  | jq '.results[0].place | {place_name, provider_id, categories, tags, location}'
```

**Expected**:
- `categories` is an **array** of `PlaceCategory` strings (was a single `place_type` string before — see contracts/http-response-parity.md).
- `tags` is an **array** of `{type, value, source}` objects (was an `attributes` map before).
- `place_name`, `provider_id`, `location` look the same as before.

## 8. Lint + type check

```bash
poetry run ruff check src/ tests/
poetry run mypy src/
```

**Expected**: clean (SC-008, SC-009).

## 9. Confirm `places_v2` was not touched

```bash
git diff dev -- src/kebi/core/places_v2/
```

**Expected**: empty diff (FR-010a — v2 is frozen).

## 10. Smoke the agent save path

```bash
curl -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "test-user",
    "message": "Save Joe & The Juice on Sukhumvit"
  }'
```

**Expected**: response contains v2-shaped place objects (matches step 7's shape). The agent recall tool is *not* yet updated; recall-of-a-just-saved-place will return empty until the follow-up feature ships — this is the intentional accepted regression (spec Assumptions).

---

## Troubleshooting

- **`ImportError: cannot import name 'PlacesSearcher'`** — anything still trying to import the deleted searcher. Search and update; the picker now consumes `list[PlaceObject]` from `PlacesSearchService.find()` directly.
- **`ValueError: provider_id must be namespaced`** — picker is emitting a bare Google `placeId`. Echo back the `provider_id` field from the search result, not `external_id`.
- **`pydantic ValidationError on PlaceCore.tags`** — picker is still emitting `attributes` dict. Update the Instructor output model per research.md R-02.
- **Legacy Redis namespace still receiving writes** — `core/extraction/persistence.py` still calls the legacy cache. Remove the call; `PlacesSearchService` handles cache writes on the cold path.
- **Partition counts diverge from baseline** — confidence-band thresholds are unchanged; if `(saved, needs_review, dropped)` differ, the picker schema change is affecting confidence numerically — check `core/extraction/confidence.py` is unchanged and only consumes numeric fields.
