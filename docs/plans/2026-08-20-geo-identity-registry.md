# Geo identity registry — identity from ids, names for display only

**Goal:** No hardcoded geography anywhere in the codebase. A place's area identity comes from a stable provider id minted once into a registry; every name a user sees comes from registry data (Google's clean component name plus a once-minted colloquial layer), never from a code table or a slug. A user anywhere on Earth gets the same correctness a user in Canggu gets today — and Canggu gets *more* correct (the Gili mislabel dies). The system degrades to coarser-but-correct, never to silently wrong.

**Architecture:** One new Alembic-owned table (`geo_areas`) is the single source of geo identity: one row per geographic unit ever seen, keyed by Google place_id, minted lazily (one Geocoding call per *unique area ever*, then free forever). Keys become id-paths; names become registry reads; the six hand-maintained fold tables and the Nominatim claims path are deleted. The `kebi://area/{token}` contract keeps its shape — the token becomes the area's place_id, with legacy slug-tokens decoded via the row's stored legacy key.

**Tech stack:** No new dependencies. Google Geocoding API (already the trusted provider for saves), one small-model LLM call per registry mint (cached forever in the row), existing rederive/claims-migration lanes (ADR-144/163 precedent).

## Problems (verified against code + live geocoder, 2026-08-20)

1. **Identity is derived from display strings — the root defect.** Keys are name slugs (`id/bali/canggu`) built by `build_geo_key`, so every way a name can vary (language, exonym/endonym, official vs colloquial, admin dress) is a data-corruption bug: claims split across keys no prefix scan joins, one area renders as two screens, library groups and counts lie.
2. **Six hand-maintained fold tables patch that defect, one incident at a time:**
   - `_CITY_ALIASES` (`core/knowledge/schemas.py:392`) — 12 city pairs (Bangkok, Jakarta, HCMC, Tokyo…)
   - `_AREA_ALIASES` (`schemas.py:309`) — 4 area pairs (Tibubeneng→Canggu, Pecatu→Uluwatu, Gili Indah→Gili Trawangan, Antwerpen→Antwerp)
   - `_TRAILING_STRIP_COUNTRIES` (`schemas.py:291`) — 6 countries get "District"/"Regency" stripped; everywhere else splits
   - `_LEADING_ADMIN_UNITS` / `_TRAILING_ADMIN_UNITS` (`schemas.py:264,284`) — covers Thai/Indonesian/Vietnamese only; Turkish, Arabic, Korean, Spanish admin words all uncovered
   - `_ADMIN_SUFFIXES` (`core/knowledge/geo_resolve.py:43`) — a *second, different* trailing-word list on the claims path
   Every table misses silently until someone notices live and ships a dict entry plus a prod rederive. Three incidents in one week (ADR-163 backfill, ADR-166 language split, Gili fold).
3. **A known-wrong fold is live.** `gili-indah→gili-trawangan` deliberately mislabels every Gili Air and Gili Meno save as Gili Trawangan; the code comment admits only coordinates could tell them apart. ADR-168 (the coordinate fix) was rolled back, so the lie is what ships.
4. **Two geocoders, two identity paths.** Saves key through Google components; research claims key through Nominatim structured lookups (`geo_resolve.py`) — each with its own folding rules, so the same place can still key differently between the paths even after every table is "complete".
5. **Coverage is a snapshot of where test users have been.** All tables together cover roughly Bali, Bangkok, Vietnam, and one Antwerp save. The product's promise is global; outside that footprint the failure mode is silent corruption, not degraded output.
6. **Stored keys go stale on every table edit.** `geo_key` is persisted (ADR-165) and SQL groups/filters on it, so a fold-table edit fixes nothing until `scripts/rederive_geo_keys.py` and the claims migration run. The Gili commit (96e9aff) shipped table+tests only — prod rederive status must be confirmed.
7. **One global component ranking, hand-tuned per country.** `_ADDR_COMPONENT_TO_FIELD` (`core/places/_google_mapper.py:254`) encodes Bali's levels, Japan's level_2, UK's postal_town as one priority list; a country where the guess is wrong stores the wrong city with no signal.
8. **Colloquial-vs-admin mismatch is unrepresentable.** Verified live: Google names Finns Beach Club's desa "Tibubeneng" and will never say "Canggu" for it — the fact "people call this whole stretch Canggu" does not exist in the geocoder. Today it's faked by the alias table (wrongly, at desa granularity); there is no data model for it.
9. **Names are guessed from slugs.** `display_from_slug` (`core/areas/keys.py:106`) prettifies key segments ("kuta-utara" → "Kuta Utara") when no area row exists — display derived from identity strings, the same defect in reverse.

## Live geocoder facts the design rests on (probed 2026-08-20)

- "Canggu" exists as `administrative_area_level_4` with its own stable place_id and the clean English name "Canggu"; a point in Canggu desa reverse-geocodes to it.
- A point at Finns (Berawa) honestly returns level_4 "Tibubeneng" — colloquial containment is not Google's to give.
- Each Gili island exists as a `natural_feature` with its own place_id ("Gili Air"), inside the "Gili Indah" level_4; coordinates disambiguate the islands.
- Component `long_name`s arrive clean in English-pinned calls ("Kuta Utara", not "Kec. Kuta Utara") — affix dress lives mostly in formatted addresses, which we don't key on.
- `addressComponents` carry **no per-component place_id** — component ids must be minted by a Geocoding lookup, which is what the registry amortises to one call per unique area ever.

## Decision set (agreed 2026-08-20)

- **Identity = Google place_id.** New `geo_areas` registry: one row per unit ever seen — place_id, kind (political unit, natural feature/island), clean English display name, colloquial layer, parent place_id chain, country code, coordinates/viewport, `legacy_key` (old slug key, for token and data compat).
- **Minting is lazy and amortised.** First save naming an unknown area triggers one Google Geocoding lookup for its id + ancestors; every later save worldwide joins by lookup. Names become lookup *hints*, never identity.
- **Ambiguity resolved by coordinates, not folds.** When the deepest component is ambiguous by containment (multi-island desa and kin — detected from registry geometry/type data, not a hand list), one reverse-geocode of the save's coordinates picks the real containing unit. Gili Air keys and displays as Gili Air.
- **Display = registry data, two layers.** Google's own component name is the honest layer; a once-per-mint LLM call records `colloquial_name` and optional `groups_into` (another row's id) — Tibubeneng's row says "groups into Canggu". Data in the DB, auditable and correctable per row; never a code table, never identity. Deliberate, display-only departure from ADR-126/160's no-LLM-naming rule, with a code backstop: LLM output is validated (existing row referenced, same country, bounded name length) before storing.
- **Keys become id-paths** (`id/{cc}/{city_pid}/{area_pid}`) so existing prefix scans, exact-key claim reads, and SQL grouping keep working unchanged.
- **Token = the area's place_id, verbatim** (`kebi://area/ChIJZZZY9GE4…`). Same opaque-segment contract (ADR-153); no client change. Decoder branch: base64-decodes-to-old-grammar → legacy token, resolved via `legacy_key`; place_id shape → new token. Old messages work forever.
- **Entities ride the display group.** An answer anchored in Tibubeneng mints its area entity from the Canggu row, so chip, screen title, and library heading agree (same doctrine as ADR-162's icon re-read).
- **Kill list:** `_CITY_ALIASES`, `_AREA_ALIASES`, `_TRAILING_STRIP_COUNTRIES`, `_LEADING_ADMIN_UNITS`/`_TRAILING_ADMIN_UNITS` (as key logic), `_ADMIN_SUFFIXES`, `display_from_slug`, and Nominatim on the claims path — the research resolver goes through the same registry. One geocoder, one identity system.
- **Invariant: mint-before-key.** A registry row exists before its key can appear anywhere; every name render is a registry read (batched, as `AreaHandleBuilder` already does). A save that cannot mint degrades to coarser-but-correct (country level / keyless bucket) — never a guessed name, and the miss is logged.
- **Cost envelope:** zero new per-save calls; one Geocoding call per unique area ever + one cached LLM mint call per row; migration backfill reuses the by-id refresh lane (unchanged field mask). Departure from prefer-free-geocoding noted and accepted: identity requires stable ids, Nominatim's are documented-unstable, and Google is already the paid provider of record for saves.

## Constitution check

- ADR-126/160 (verified-or-refuse, no LLM naming): DEPART, display layer only — identity remains geocoder-verified ids; the LLM touches `colloquial_name`/`groups_into` with a code-validated output. Record in the new ADR.
- ADR-144 (canonical city slug + migrate lane): SUPERSEDED — alias tables retired by ids; its data-only migration lane is reused as-is.
- ADR-153 (encoded area URIs, codec in `keys.py`): COMPLY — same opaque-segment contract, codec stays the single wire-format authority, gains the legacy branch.
- ADR-163 (human-named stored geo): SUPERSEDED IN PART — component ranking survives as the *hint* for which unit to mint; folding/stripping as key logic dies.
- ADR-165 (stored geo_key, library by area): COMPLY — column re-derived to id-paths via the same rederive lane; grouping semantics unchanged.
- ADR-136/158/159 (chat contract, streaming, links on terminal frame): COMPLY — untouched; agent prose and DeltaBuffer see only better names.
- ADR-120 (exact-key claim reads): COMPLY — claims migrate to id-keys in one data-only migration; no stranded keys.
- Feedback rules: explicit mechanisms (mint-before-key invariant, validated LLM output, logged degradation); provider-agnostic deps (registry mint behind the existing geocoding protocol/adapter); prompt rules get code backstops; no feature loss.

## Steps

### Step 1 — registry
- [X] `geo_areas` + `geo_area_aliases` tables + Alembic migration (schema-only, ADR-166 rule); repository; mint service. Design deltas discovered live: ambiguity is resolved by LLM-minted split rows + point-in-viewport geometry (reverse geocoding does NOT recover islands — verified against the live API); verification uses the provider's `partial_match` exactness signal instead of string heuristics (exonyms resolve exactly, typos arrive flagged — no word lists, and no Kansas/"Kansas City" trap); `part_of` groups are code-backstopped against the model's admin-parent habit ("Canggu is part of Bali/Badung" is refused). `area_registry` role runs gpt-4o, not mini — mini missed the multi-island `covers` case outright in live probes, and the call fires once per unique area ever.
- [X] Verify: unit tests over an in-memory repo + scripted lookup (Bangkok merge, Muine refusal, Tibubeneng fold, Gili splits, legacy keys) — green; live mint smoke against real Google + LLM + local DB: Canggu ✓, Tibubeneng→Canggu ✓, Gili Air point→Gili Air ✓, Gili T point→Gili Trawangan ✓, Bangkok variants→one key ✓.

### Step 2 — keys and codec
- [X] `key_for_location` on the registry replaces `build_geo_key`/`geo_key_for_location`; token stays base64url of the (now id-path) key — self-describing, same opaque contract, no client change; legacy decode branch (`is_legacy_geo_key`) + registry `legacy_key` column and `legacy`-scoped alias rows for multi-variant old keys.
- [X] Verify: codec round-trip + legacy-detection tests green; areas route translates legacy tokens (2 new tests).

### Step 3 — write paths and readers
- [X] Save path (PlaceUpsertService resolves + stores `PlaceCore.geo_key`), claims writer, research resolver, harvesters, curator, curation anchors, entity search, candidate notes, entity minting (async pre-resolved pairs; entities ride the display group), handles/screens/profiler (names from registry rows), library free-text search (matches registry names via EXISTS instead of key-text). `display_from_slug` and `geo_resolve.py` (EntityGeoResolver) deleted; Nominatim stays only for per-turn coordinates/density (agent resolver, HomeService).
- [X] Verify: full test suite updated and green across areas/places/knowledge/api/chat/agent.

### Step 4 — delete the tables
- [X] All six fold tables gone (`_CITY_ALIASES`, `_AREA_ALIASES`, `_TRAILING_STRIP_COUNTRIES`, both affix lists, `_ADMIN_SUFFIXES`); the only surviving word lists are `core/geo/display.py` (display-only affix strip, zero key effect). Historical Alembic data migrations carry frozen snapshots of the old fold logic so `alembic upgrade head` works forever on fresh databases.
- [X] Verify: grep proves zero place-name identity literals in src.

### Step 5 — migration
- [X] `scripts/migrate_geo_identity.py` (network mints → legacy-key mapping → places/claims/areas rewrite, idempotent, `--dry-run`); `scripts/rederive_geo_keys.py` rewritten as the registry-correction maintenance lane; runbook at `docs/runbooks/geo-identity-migration.md` (includes the pre-check on whether the Gili-commit rederive ever ran in prod).
- [X] Verify: run against local DB — zero slug keys left on places/claims; Gili/Canggu resolution spot-checked in SQL.

### Step 6 — end-to-end + record
- [X] Live verification (registry level): Tibubeneng saves key as Canggu, Gili points split per island, legacy key lookup resolves.
- [X] ADR-169 in `docs/decisions.md`; ruff + mypy (baseline-only remainder) + pytest. Merge to `dev` after owner review; prod migration run per runbook (ask-first).
