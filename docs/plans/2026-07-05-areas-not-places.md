# Areas Are Not Places — Type-Based Drop at the Provider Boundary

**Branch:** `fix/areas-are-not-places` (from `dev`)
**Status:** in progress

## Problem

Shared videos often mention geographic areas (neighborhoods / districts / cities / countries: "Dubai Marina", "Palm Jumeirah", "JBR", "Japan") alongside real venues. These areas leak through extraction and get saved and shown as venue cards — dead pins with no category. They also drive miscategorization: Google returns administrative types (`["neighborhood", "political", "geocode"]`) that `map_place` doesn't recognize, so `categories=[]` and the LLM picker — with no area vocabulary and a "name ending in 'Beach' → beach" nudge — labels Jumeirah Beach Residence a `beach`.

**Root cause:** `map_place` (`src/kebi/core/places/_google_mapper.py`) reads Google's `types[]` and silently discards the administrative types — the venue-vs-geography distinction dies at the provider boundary. The downstream `drop_geographic_features` name-regex (`src/kebi/core/extraction/geo_filter.py`) is a weak proxy that misses keyword-less names like "Dubai Marina".

## Decision

An area is never a product object — not saved, not shown, not picked, at any scale (neighborhood through country). Administrative results are detected via Google's `types[]` and refused at map time, so they never become `PlaceObject`s anywhere in the system. Location scoping is unaffected: the resolver's `query_locations` text hints scope venue searches independently of area results. Pure-area videos yield an empty extraction (accepted). No schema changes, no new fields.

## Phases

### Phase 1 — `_google_mapper.py`: refuse administrative results

Commit: `fix(places): refuse administrative Google results at map time`

- [ ] Add module-level frozenset `_ADMINISTRATIVE_TYPES`: `locality`, `sublocality`, `sublocality_level_1..5`, `neighborhood`, `political`, `administrative_area_level_1..7`, `country`, `postal_code`, `postal_town`, `route`, `street_address`, `intersection`, `plus_code`, `premise`, `subpremise`, `geocode`, `colloquial_area`, `archipelago`, `continent`.
- [ ] Add public helper `is_administrative(types, categories) -> bool` (reused by the Phase 4 script): administrative when (a) `types[0]` is in the set (Google's most-specific-first contract), or (b) no venue category mapped AND any type is in the set. A real beach (`["beach", "natural_feature"]`) maps to `beach` and survives; Palm Jumeirah (`["neighborhood", "political", "geocode"]`) maps to nothing and dies.
- [ ] In `map_place`, right after `categories` is computed: `if is_administrative(types, categories): return None`. The only caller already filters None (`google_client.py` `get`/search mapping).

### Phase 2 — delete `geo_filter.py` (dead code once Phase 1 lands)

Commit: `refactor(extraction): retire name-regex geo filter`

- [ ] Delete `src/kebi/core/extraction/geo_filter.py`.
- [ ] `extraction_pipeline.py`: remove import; `for place in drop_geographic_features(results):` → `for place in results:`; update module docstring (search step now notes the places module refuses admin results at map time).
- [ ] `discover_places_tool.py`: remove import; use `hits` directly instead of `drop_geographic_features(hits)`.

### Phase 3 — picker prompt/schema: remove area coping language

Commit: `fix(extraction): drop area escape-hatch from picker vocabulary`

- [ ] `llm_picker.py` `_PickedPlace.categories` description: drop "Leave empty only when nothing in the vocabulary fits (e.g. a whole town, island, or region)" → "leave empty only when genuinely nothing fits".
- [ ] `config/prompts/place_classifier.txt`: same sweep; keep the "'Beach' → beach" example only if scoped to actual beaches.
- [ ] Prompt changed → note Redis LLM-cache implications (cached picker responses keyed on old prompt are stale but harmless; they expire).

### Phase 4 — `scripts/purge_area_places.py`: legacy cleanup

Commit: `chore(db): one-off purge of area rows saved as places`

- [ ] For each `places` row: fetch raw Google details by `provider_id` (raw `types[]` — `get_by_ids` now maps areas to None, so fetch at the raw layer) and call `is_administrative` on the raw types.
- [ ] Dry-run report by default; `--apply` to delete matched rows from `places` + dependents (`user_places`, `place_embeddings`; FK order). Reuse session patterns from existing `scripts/` (e.g. `places_smoke.py`).

### Phase 5 — tests

Commit: `test(places): administrative refusal + pipeline area-only case`

- [ ] Mapper: neighborhood → None, country → None, route → None, real beach → kept (`beach`), venue with trailing admin type → kept.
- [ ] Pipeline: post whose only known place resolves to an area → empty picks, no persistence.
- [ ] Rework `tests/core/agent/tools/test_discover_places.py::test_geo_filter_drops_administrative_only_hits` — guarantee moved to the mapper.

### Phase 6 — ADR + verify

Commit: `docs(api): ADR-115 — geographic areas are not places`

- [ ] Append ADR-115 to `docs/decisions.md` (decision altitude: problem, decision, consequences).
- [ ] All checkboxes above checked; plan marked complete.

## Out of Scope

- No `kind`/`area_scale` fields, no area categories — the model needs no vocabulary for what it refuses.
- No consult/chat-layer acknowledgment of area-only videos — empty result is fine.
- No area-derived taste signal.

## Verify

```bash
poetry run pytest
poetry run ruff check src/ tests/
poetry run mypy src/
```

End-to-end:

1. `POST /v1/extract` with text referencing "Dubai Marina" + one real venue → venue only; no new area row in `places`.
2. Area-only payload ("Dubai Marina is stunning") → empty extraction, clean 200.
3. Purge script dry-run against dev DB → report lists Dubai Marina / Palm Jumeirah / JBR-style rows; `--apply`; `GET /v1/user/library` shows no dead pins.
