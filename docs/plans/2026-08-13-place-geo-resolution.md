# Place geo resolution — human-named, complete, one key per place

**Goal:** Every save lands on exactly one area screen, under the name a human would use — no invisible saves, no administrative names where a colloquial locality exists, no key splits between the place catalog and the claims/areas already keyed colloquially.

**Architecture:** The fix lives at the single point provider geo becomes stored location (the provider adapter's component parser) and the single point locations become keys (`build_geo_key`). The screen service stays a pure reader. A one-off backfill re-fetches broken rows through the existing by-id refresh and a data-only migration re-keys the handful of affected claims/area rows (ADR-144 lane).

**Tech stack:** No new dependencies. Existing Google Places by-id refresh (unchanged field mask), existing transliteration + slugging.

## Problem (verified live, 2026-08-13)

1. **Invisible saves.** 214/678 place rows (written ≤ 2026-07-09, before ADR-119's ranked fallback) lack `country_code`/`city`. The area screen skips them silently — "Kalà Kalà Beach Club" appears on no screen and isn't counted. ADR-119's TTL self-heal has not reached them in 5 weeks.
2. **Administrative names.** In regions where Google returns no `locality` (Bali, Bangkok, much of VN/TH/ID), the fallback puts an admin unit in the neighborhood slot: a Canggu save stores `neighborhood="Kabupaten Badung"` (admin_2) even though `Canggu` (admin_4) is present in the same component list. The Bali drill-down row reads "Kabupaten Badung". Meanwhile the chat path already keys `id/bali/canggu` (claims + a profiled area row exist) — one area, two keys, the exact split ADR-153 flagged.
3. **Admin-unit words in names/slugs.** "Thành phố Huế" vs "Hue", "Kota Denpasar", "Khet Bang Rak", "Ko Samui District" — the unit word both reads non-human and splits slugs that transliteration alone cannot fold.

## Decision sketch

- **City slot:** unchanged ranking (locality → postal town → admin levels, per ADR-119), plus admin-unit-word stripping before display/slug.
- **Neighborhood slot:** re-ranked most-specific-human-first across the component list: `neighborhood` → `sublocality_level_1` → `sublocality` → `admin_area_level_4` → `admin_area_level_3` → `admin_area_level_2`, never the component already used as the city, never level 1. Verified against live geocoder output: Bali → "Canggu" (not regency), Bangkok → "Khet Bang Rak" → stripped "Bang Rak", Da Nang → "Ngũ Hành Sơn" (unchanged), Samui → "Bo Put".
- **Admin-unit stripping:** a small hand-maintained affix list (leading: Thành phố, Kota, Kabupaten, Kecamatan, Kec., Khet, Khwaeng, Tambon, Muang/Mueang…; trailing: District, Regency, City…) applied at the parser (stored display geo is already human) and defensively at slug time (`build_geo_key`) so pre-existing stored values fold too. Same maintenance model as ADR-144's `_CITY_ALIASES`; ADR-160 already tolerates trailing admin-unit words as a match rule.
- **Anchoring follows the geocoder's hierarchy** (verified-or-refuse doctrine, ADR-126/160): Hội An inside Đà Nẵng is Google's post-2025-merger admin truth; the screen reads "Da Nang › Hội An", which is humane. We do not re-invent containment.
- **Migration, not orphaning** (ADR-144 lane, explicitly departing ADR-125/130's orphan default): the store is no longer thin, and reads are exact-key (ADR-120), so stranded keys silently hide knowledge. Blast radius measured: 7 neighborhood-depth claims, 11 area rows (1 stranded: `id/bali/kabupaten-badung`), 214 place rows.
- **Backfill via existing by-id refresh** — same field mask, no billing-tier change (ADR-118/119 joint rule). Departure from ADR-119's TTL self-heal is deliberate: it demonstrably hasn't fired for read-only rows.

## Constitution check

- ADR-118 (minimal validator): COMPLY — field mask untouched; backfill uses the existing by-id refresh.
- ADR-119 (ranked fallback): SUPERSEDED IN PART by new ADR-163 — neighborhood ranking re-ordered, affix stripping added, explicit backfill replaces TTL self-heal for the stranded cohort.
- ADR-120 (exact-key claims reads): COMPLY — migration keeps claims reachable; no read changes.
- ADR-122/ADR-022 (provider abstraction): COMPLY — component semantics stay in the Google adapter.
- ADR-124 (admin areas never savable): COMPLY — untouched.
- ADR-125 (transliterate-then-slug): COMPLY — stripping happens before slugging; transliteration unchanged.
- ADR-126/160 (verified-or-refuse, never re-geocode anchors): COMPLY — no free-text lookup, no LLM naming; only components already in the verified response are used; hierarchy is the geocoder's.
- ADR-144 (canonical city slug + data-only migration): FOLLOWED as the template, extended to the neighborhood slot exactly as ADR-153 requested.
- ADR-146/162 (icons by key/row): CONSEQUENCE — re-keyed areas borrow icons under the new key; stranded row deleted so chips and rows can't diverge.
- ADR-153 (area screens, mechanical child keys): COMPLY — closes its named standing gap; key grammar and depth semantics unchanged.

## Phases

### Phase 1 — resolution fix at the write point
- [X] Add admin-affix strip helper + neighborhood re-ranking in the provider adapter's component parser
- [X] Apply defensive strip in `build_geo_key` slugging for both city and neighborhood parts
- [X] Unit tests: Bali/Canggu, Bangkok/Khet, Hue/Thành phố, Samui/District, Da Nang unchanged, locality-present countries unchanged
- Files: provider adapter parser, `core/knowledge/schemas.py`, tests
- Verify: `poetry run pytest tests/ -x -q -k "geo or location or key"`

### Phase 2 — backfill + data migration
- [X] Script `scripts/backfill_place_geo.py`: rows with missing `country_code`/`city` OR admin-named neighborhood → by-id refresh through the fixed parser; report per-row before/after
- [X] Data-only migration for claims (`entity_key`) and `areas.geo_key` affected by re-slugging; delete stranded `id/bali/kabupaten-badung` row
- [X] Run against local DB
- Verify: SQL — zero saved places skipped by the screen's key builder; no claims under dead keys

### Phase 3 — end-to-end verification
- [X] Real service calls: Da Nang screen counts Kala Kala; Bali screen shows "Canggu (6)"; totals match reality
- Verify: scripted calls against local DB (screen service), plus SQL cross-check

### Phase 4 — record + merge
- [X] ADR-163 in `docs/decisions.md` (decision altitude)
- [X] `poetry run ruff check src/ tests/ && poetry run ruff format src/ tests/ && poetry run mypy src/ && poetry run pytest`
- [X] Merge `fix/place-geo-resolution` → `dev`

## Verify all
`poetry run pytest && poetry run ruff check src/ tests/ && poetry run mypy src/` + Phase 2/3 SQL checks.

## Completion notes (deviations from the sketch)

- Trailing "District"/"Regency" strip is **country-gated** (id/th/vn/la/kh/mm) — live data showed "Financial District" (us) must survive; the suffix strips only where it is Google's translation of a local unit.
- Added `chang-wat`/`changwat` to the leading affixes after the backfill surfaced "Chang Wat Surat Thani" splitting Surat Thani.
- Added a neighborhood **alias table** (`tibubeneng→canggu`, `pecatu→uluwatu`): Google's village grid is finer than colloquial areas; ADR-144's instrument extended to the neighborhood level as ADR-153 asked.
- Google client now pins `languageCode=en` on every call (same rule as Nominatim's accept-language) — without it re-fetches returned locale-dependent names and split keys.
- Merge policy: legacy location blobs (no country_code) upgrade wholesale on any fresh fetch — makes ADR-119's promised self-heal real, not just the one-off backfill.
- Backfill retries batch-unpairable (remapped-id) rows one-by-one; 3 rows with dead provider ids remain legacy (zero saves on them).
- Verified live against the real screen service: Da Nang counts 4 (Kala Kala under Ngu Hanh Son), Bali shows Canggu (5) + Uluwatu (3).
