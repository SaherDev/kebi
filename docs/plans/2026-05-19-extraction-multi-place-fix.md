# Plan: Fix multi-place extraction collapse + LLM-resolve-before-search

**Date:** 2026-05-19
**Status:** proposed — awaiting approval

## Problem

Posts with ≥2 places (TikTok/IG carousels, Google Maps shared lists,
multi-restaurant videos) return only **one** place. Reproduced with
`https://www.tiktok.com/@withme808/photo/7620175392019664161` (5
restaurants → 1 returned).

### Root cause (proven)

Vision correctly extracts all 5 names; all 5 reach
`context.known_places`. `ExtractionPipeline._extend_search_set`
(`extraction_pipeline.py:284,301`) fans out one
`PlacesSearchService.find()` per name **in parallel** via
`asyncio.gather` (semaphore `_SEARCH_CONCURRENCY = 5`), but every
`find()` shares **one request-scoped `AsyncSession`**
(`PlacesRepo(db_session)` ← `Depends(get_session)`). SQLAlchemy
`AsyncSession` is not concurrency-safe — 4 of 5 queries die with
`InvalidRequestError: This session is provisioning a new connection;
concurrent operations are not permitted`, get swallowed by the
best-effort `except` at `extraction_pipeline.py:294`, degrade to `[]`.
Only the name already in the DB (`Restaurant POTONG`, the 2026-05-14
row) survives → `search_set` has 1 → picker returns 1.

Not cache-, TikTok-, or vision-related. Latent across all multi-place
inputs.

## Constraints (from docs/decisions.md)

- **ADR-070 (binding):** all lookups go through `PlacesSearchService`;
  the LLM must never invent venues — final `provider_id` comes from a
  real `find()`.
- **ADR-071 (binding):** every picker output saved `approved=False`.
- **ADR-059 (binding):** new prompts in `config/prompts/`, logical
  name in `app.yaml` under `models:`/prompt config.
- No binding ADR locks "search-first" ordering — reorder is allowed.
- Pydantic at boundaries; provider abstraction; mypy strict
  (`CLAUDE.md`).

## Design

Two independent changes. (A) is the bug fix and is sufficient on its
own to restore all 5 places. (B) is the quality redesign the user
asked for and depends on (A) staying in place (its fan-out is still
parallel).

### A. Concurrency fix — session-per-query fan-out

`_extend_search_set`'s parallel `find()` calls must not share one
session. Make each concurrent query acquire its own `AsyncSession`
from the session factory; `PlacesSearchService`/`PlacesRepo` for the
fan-out are constructed per-task against that session. The single
request-scoped session stays for the rest of the pipeline.

Open seam to confirm in implementation: `PlacesSearchService` is
injected already-constructed (bound to one session). Cleanest fix is
to inject a *factory* (`Callable[[], PlacesSearchService]` /
`async with session_factory()`) for the fan-out path only, or move
the per-query session scope into `PlacesSearchService.find`. Decide
after tracing how the service threads the session through
DB/cache/Google/upsert (one Explore pass).

Acceptance: the repro returns all 5; no `places_search_failed`
warnings; existing extraction tests green.

### B. LLM-resolve-before-search (quality)

Split today's single `LLMPlacePicker` into two passes:

1. **Resolver (pre-search, new):** input = `known_places` (raw vision/
   gmaps names) + caption + hashtags + title + supplementary. Output:
   - per candidate: a cleaned search query string;
   - one **shared `LocationContext`** inferred for the whole post
     (e.g. `#bangkok` + "5 Top-Restaurants in Bangkok" →
     `city="Bangkok", country="Thailand"`);
   - one set of **shared post-level `PlaceTag`s** derived from the
     overall picture, not any single venue (e.g. a "5 top fine-dining
     in Bangkok" post → shared `atmosphere=upscale`,
     `price=very_expensive`, `time=dinner`). The classifier merges
     these into every pick alongside per-place tags (dedupe by
     `(type,value)`, `source="llm"`); per-place evidence still wins on
     conflict.

   Also drops non-place noise. Prompt in `config/prompts/`.
2. **find():** `PlaceQuery(place_names=[query],
   location=<shared LocationContext>)` — location-biased text search,
   better Google disambiguation. Fan-out uses (A)'s session-per-query.
3. **Classifier (post-search):** the existing picker's
   pick/validate/tag/reject half, fed real `PlaceObject`s. Still
   references real `provider_id`s (ADR-070), output saved per ADR-071.

Cost: 2 LLM calls per level instead of 1 (resolver + classifier).
Accept (better recall + precision on multi-place posts) — note in the
ADR.

New ADR required (decision altitude, no impl detail): "Resolve-then-
search: a pre-search LLM pass enriches queries with post-level shared
context (location + shared attribute tags) inferred from the whole
post; the picker becomes a post-search classifier that merges shared
context into every pick."

## Checklist

- [ ] A1. Explore: trace `PlacesSearchService.find` session threading
      (DB-first / cache / Google / upsert) — pick the fix seam.
- [ ] A2. Implement session-per-query fan-out in `_extend_search_set`
      (+ wiring in `deps.py`).
- [ ] A3. Tests: multi-name fan-out returns all names; concurrency
      regression test (≥2 names, no `InvalidRequestError`).
- [ ] A4. Verify: repro script returns 5; `pytest`, `ruff`, `mypy`.
- [ ] B1. Write ADR (resolve-then-search) in `docs/decisions.md`.
- [ ] B2. Resolver prompt → `config/prompts/`, logical name in
      `app.yaml`.
- [ ] B3. Implement resolver pass; refactor `LLMPlacePicker` →
      post-search classifier; rewire pipeline ordering.
- [ ] B4. Update `docs/architecture.md` / pipeline docstring
      (search-first → resolve-then-search).
- [ ] B5. Tests for resolver (location + shared-tag inference from
      hashtags/caption) + classifier shared-tag merge; full
      pipeline e2e.
- [ ] B6. Verify: `pytest`, `ruff`, `mypy`; repro returns 5 with
      location-biased matches.

## Verify commands

```
poetry run pytest tests/core/extraction -x
poetry run ruff check src/ tests/
poetry run mypy src/
poetry run python /tmp/diag_pipeline.py   # repro: expect 5 places
```

## Sequencing recommendation

Land **A** first (small, isolated, fixes the production bug now),
then **B** as a separate change behind its own ADR. Combining them
risks coupling a critical bug fix to a larger redesign.
