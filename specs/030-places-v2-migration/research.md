# Phase 0 Research — Extraction → Places v2 Cutover

Resolutions for every open decision behind the implementation. Each section is **Decision / Rationale / Alternatives considered**. `[res]` markers refer to questions raised during planning.

---

## R-01 — Legacy → v2 type mapping table

**Decision**: Adopt the following field-level mapping. This is the single source of truth referenced by `data-model.md` and by every PR in this migration.

| Legacy (in `core.places`) | v2 (in `core.places_v2`) | Notes |
|---|---|---|
| `PlaceObject` (full, legacy fields) | `PlaceObject` (extends `PlaceCore`) | Drop legacy fields not present in v2; add `cached_at`, `business_status`, `popularity`. |
| `PlaceCreate` | `PlaceCore` | v2 writes via `PlacesRepoProtocol.upsert_places(cores)` and via `PlaceUpsertServiceProtocol.upsert_many(cores)`. |
| `PlaceType` (single enum) | `PlaceCategory` enum, used as `list[PlaceCategory]` | v2 stores a *list* of categories (multi-category support — see `places_v2/models.py:266` + `a4d2c1b9e8f3` migration). Legacy single-type maps to a one-element list. |
| `PlaceAttributes` (free-form structure) | `list[PlaceTag]` | Each `PlaceTag` carries `type` (TagType: cuisine / dietary / feature / atmosphere / service / price / accessibility / time / season), `value` (enum or free-text), and `source` (`"google" | "llm" | "manual" | ...`). Convert legacy attribute fields into one tag per non-null axis. |
| `PlaceProvider` + `external_id` (two-field identity) | `provider_id: str` (namespaced) | Format: `"<provider>:<external_id>"` — validated by `PlaceCore._validate_provider_id` (must contain `":"`). For Google: `"google:<placeId>"`. Helper constant in `core/places_v2/_google_mapper.py:GOOGLE_PROVIDER_PREFIX`. |
| `PlaceSource` (legacy enum) | `PlaceSource` (v2 enum) | v2 enum values: `tiktok`, `instagram`, `youtube`, `google_maps_list`, `manual`, `kebi`. The v2 enum already includes the rebranded `kebi` value (commit 4c0608d). Drop the legacy import everywhere in extraction. |
| `PlacesMatchQuality` (legacy) | _no v2 equivalent_ — replaced by the geo-feature filter | The legacy searcher used `match_quality` to drop NONE / CATEGORY_ONLY results. With `PlacesSearchService` as the source of truth, that signal disappears and a thin filter (`persistence_geo_filter.py`) drops geographic-feature `PlaceCategory` results instead. See R-04. |
| `build_provider_id(provider, external_id)` (legacy helper) | `f"{prefix}{external_id}"` using `GOOGLE_PROVIDER_PREFIX` | Convention is `"google:"` namespace prefix; no helper function needed — string concatenation is fine. |
| `LocationContext` (legacy) | `LocationContext` (v2) | Field names compatible: `lat`, `lng`, `address`, plus v2-only `radius_m`, `neighborhood`, `city`, `country`. Extraction only emits `lat`/`lng`/`address`. |
| `HoursDict`, `BusinessStatus`, `cached_at`, `rating`, `phone`, `popularity` | v2 `PlaceObject` live fields | Populated by `core/places_v2/_google_mapper.map_place()` from Google response; extraction never builds these by hand. |

**Rationale**: v2 vocabulary is richer (multi-category, tag-axis taxonomy, namespaced identity) and the migrations are already in production schema. Forcing extraction to write through `PlaceUpsertService` (per ADR-054 generalized to v2) means merge policy stays centralized in `_place_merge.py`.

**Alternatives considered**:
- Build a transitional `LegacyPlaceAdapter` keeping both shapes alive — rejected: violates FR-009 (no aliasing legacy types) and ADR-070 (single source of truth).
- Skip multi-category for now and emit a single `PlaceCategory` — rejected: v2 schema requires a list, and the upsert path expects one.

---

## R-02 — LLM picker (`enrichers/llm_picker.py`) output schema

**Decision**: Reshape the Instructor output Pydantic model to:

- Replace `place_type: PlaceType` → `categories: list[PlaceCategory]` (LLM emits 1–3 categories per pick, ordered most-specific first).
- Replace `provider: PlaceProvider` + `external_id: str` → `provider_id: str` (already namespaced — the picker echoes back what `PlacesSearchService.find()` returned).
- Replace `attributes: PlaceAttributes` → `tags: list[PlaceTag]` (LLM emits tags inferred from caption / transcript / title / hashtags; each tag stamped with `source="llm"`).
- Keep: `confidence`, `evidence_fields` (which medium supported the pick), `subcategory` (free-text refinement, becomes a `tag(type="subcategory", source="llm")` at the boundary), `place_name`.

The prompt itself is rewritten to describe v2 vocabulary: list the allowed `PlaceCategory` values, list `TagType` axes with their enum value sets, instruct the LLM to choose the picker's input set by `provider_id`.

**Rationale**: FR-008 requires v2 vocabulary inside extraction. FR-002 requires preserved partition behavior, which depends on `confidence` numeric values being comparable to the pre-migration baseline. Confidence is computed in `core/extraction/confidence.py` (numeric only, no place vocabulary), so re-shaping the surrounding fields does not affect partitioning. The categorical-output drift is a deliberate accepted change (see R-09 verification approach).

**Alternatives considered**:
- Keep legacy vocabulary in the LLM prompt, translate output to v2 on the boundary — rejected: FR-009 forbids aliasing; the prompt would then carry vocabulary not used anywhere else in the codebase and accuracy drift would still need verification.
- Force a single category (preserve legacy single-type semantics) — rejected: v2 schema supports multi-category and so will the picker.

---

## R-03 — `reconcile_picks` join key

**Decision**: Join picker output to `PlacesSearchService.find()` results by `provider_id` (namespaced string). The "echoed fields win" rule from the legacy searcher simplifies: since the service result is already authoritative (Google data via `_google_mapper.map_place()`), the picker contributes only `categories`, `tags`, `subcategory`, `confidence`, and `evidence_fields`; `place_name`, `provider_id`, `location` are taken from the service result directly. Picks whose `provider_id` is not in the search-result set are dropped (preserving the safety-net behavior).

**Rationale**: ADR-070 makes `PlacesSearchService` authoritative. Joining by namespaced `provider_id` is unambiguous (no need to carry provider enum separately).

**Alternatives considered**: None — keeping any legacy field as join key would require carrying `PlaceProvider` into the picker shape, which FR-008 forbids.

---

## R-04 — Geographic-feature filter location

**Decision**: Move the legacy `_drop_geographic_features` filter (currently inside `core/extraction/searcher.py`) into a new file `core/extraction/persistence_geo_filter.py`. It's a thin pure function `drop_geographic_features(places: list[PlaceObject]) -> list[PlaceObject]` applied to the output of `PlacesSearchService.find()` before handing to the picker. The Google `place_types` field is no longer surfaced through v2's `PlaceObject` (v2 already abstracts this away to `PlaceCategory`), so the filter now keys on v2 `PlaceCategory` membership: drop results whose categories list is empty *and* whose name matches an administrative-only heuristic (city / country / route — small allow-list at the top of the file).

**Rationale**: v2 module is frozen (FR-010a), so the filter cannot live in v2. The behavior is extraction-specific ("savable venue" semantics), not generic search. A small focused file keeps it discoverable.

**Alternatives considered**:
- Embed the filter inline in `extraction_pipeline.py` — rejected: the function has its own test surface and a dedicated module makes the test easier to keep alive.
- Replace with confidence-threshold-only filtering — rejected: the LLM picker tends to pick administrative names when no real venue matches, and confidence alone is not a reliable rejection signal for that failure mode.

---

## R-05 — `ValidatedCandidate` reshape

**Decision**: New shape in `core/extraction/types.py`:

```
@dataclass
class ValidatedCandidate:
    place_name: str
    provider_id: str          # namespaced
    categories: list[PlaceCategory]
    tags: list[PlaceTag]
    confidence: float
    evidence: list[Evidence]
    subcategory: str | None = None
    location: LocationContext | None = None   # echoes Google's lat/lng/address from the search service
```

`ExtractionContext.source` type changes from legacy `PlaceSource | None` to v2 `PlaceSource | None`. `ExtractionContext.known_places`, `text_evidence`, `image_urls`, etc. keep their current shapes (pipeline state, not place identity).

`SearchMatch` is deleted; nothing inside `ExtractionContext` references the search set as a typed list anymore — the search is now a transient call producing `list[PlaceObject]` consumed inline by the picker.

**Rationale**: Smallest shape that carries everything the persistence layer needs to upsert into v2 (provider_id + categories + tags + location + name) plus the audit trail (`evidence`) and the partitioning key (`confidence`). All field names match v2 conventions.

**Alternatives considered**: Keep `ValidatedCandidate` as it is and convert at the persistence boundary — rejected: every intermediate caller would still need legacy types, violating FR-008.

---

## R-06 — Persistence: drop `ExtractionPersistenceService`, inline v2 service calls

**Decision** (per ADR-071): Delete `core/extraction/persistence.py` entirely. `ExtractionService.run` makes three calls in sequence after the picker → dedup → geo-filter step:

1. **`PlaceUpsertService.upsert_and_embed(cores)`** — DB upsert and embedding in one v2 call. The merge policy (sticky name, deduplicated aliases and tags, union of categories) runs inside; embeddings are upserted into `place_embeddings_v2` with diff-then-embed semantics. Returns `list[PlaceCore]` with `id` populated.
2. **Pre-filter duplicates via `UserPlacesRepo.get_existing_place_ids(user_id, place_ids)`** — returns the ids the user already has saved. Caller splits the persisted cores into `to_link` and `duplicates`.
3. **`UserPlacesService.save_places(user_id, to_link, source, source_url)`** — links new places to the user with `approved=False`. Returns `list[UserPlace]`. `DuplicateUserPlaceError` should not fire because of the pre-filter; if it does, treat as a logic bug.
4. **`EventDispatcher.dispatch(PlaceSaved(...))`** — one event per newly linked place, fires the same taste-model regen and memory-extraction handlers as before.

Confidence band partitioning is removed entirely (ADR-071 supersedes ADR-057). All picker outputs are saved; the user curates after via the user-places approve/delete API. Confidence is still computed by `core/extraction/confidence.py` but it's metadata, not a write gate.

Removed responsibilities (all gone — not moved elsewhere):
- Partition by confidence band.
- Separate DB write — `upsert_and_embed` is the single write path.
- Separate Tier 2 geo cache write — `PlacesSearchService` handles cache on the cold path.
- One-by-one duplicate retry loop — v2 services merge by `provider_id` and the user_places pre-filter handles user-level dedup.
- The `ExtractionPersistenceService` class and `core/extraction/persistence.py` file.

**Rationale**: With both ADR-070 (single source of truth for lookups) and ADR-071 (save-all), the extraction-side persistence layer becomes three v2 service calls + one event dispatch. That's a fragment, not a service — it lives inline in `ExtractionService.run`. Removing the partition simplifies the contract (no `needs_review` band to surface externally) and aligns with v2's built-in `approved=False` tentative semantic.

**Alternatives considered**:
- Keep `ExtractionPersistenceService` as a thin orchestrator class — rejected: it would wrap three calls and emit one event; a class adds no testable behavior beyond what `ExtractionService.run` already provides.
- Catch `DuplicateUserPlaceError` and retry instead of pre-filtering — rejected: pre-filter is a single repo call with no exception-as-control-flow overhead; clearer in tests too.
- Preserve the partition for one release while building the save-all path — rejected: spec is a clean-cut migration, not a coexistence design (Q1 clarification, ADR-071).

---

## R-07 — `core/extraction/dedup.py` migration

**Decision**: Reduce to a single function `dedup_by_provider_id(candidates: list[ValidatedCandidate]) -> list[ValidatedCandidate]` that takes the highest-confidence candidate per `provider_id`. Drop the legacy imports (`PlaceAttributes`, `build_provider_id`). The function is still useful because the pipeline runs the search + picker step at multiple `EnrichmentLevel`s and the same place can be picked twice.

**Rationale**: Simple, single-purpose, no place-identity construction needed (the picker already received `provider_id` from the search service).

**Alternatives considered**: Move dedup into `extraction_pipeline.py` directly — possible, but the function is cleaner as a free function and has its own test.

---

## R-08 — Embedding flow

**Decision**: Call `EmbeddingService.embed_and_store(cores)` once per extraction request, after upsert, on the saved + needs_review cores. The service handles:
- Builds text from `PlaceCore` (name + aliases + categories + tags + location-as-prose) — already implemented.
- Computes SHA-256 hash, checks `EmbeddingsRepo.get_signatures_by_place_ids()` for existing matching (hash, model) — skips re-embed.
- Calls `EmbedderProtocol.embed(texts, input_type="document")` — uses the configured `embedder` role.
- Upserts vectors into `place_embeddings_v2` via `EmbeddingsRepo.upsert_embeddings()`.

`config/app.yaml` `models.embedder` role is unchanged (Voyage `voyage-4-lite`, 1024 dims).

**Rationale**: v2 already provides exactly what extraction needs. The "document" input type aligns with how the hybrid search service later embeds queries with `"query"` (asymmetric, by design — see `places_v2/embedding_service.py` docstring).

**Alternatives considered**: Skip embedding for `needs_review` candidates to save Voyage credits — rejected: needs_review rows are visible to recall once read paths migrate, so they need embeddings too.

---

## R-09 — Behavioral parity verification (fixture set + baselines)

**Decision** (updated after ADR-071):

1. **Save-count floor (SC-007)**: Capture a JSON fixture of ~20 representative extraction inputs (mix of TikTok, Instagram, YouTube, Google Maps lists, manual text). Run extraction before the cutover and capture per-fixture `(saved_count, needs_review_count, dropped_count)` from the partition logic; store under `tests/core/extraction/fixtures/v2_cutover/baselines/partition_counts.json`. After the cutover, re-run and capture `saved_count` (no `needs_review` anymore — it's a deprecated emitted value). Assert: post-cutover `saved_count >= pre-cutover (saved_count + needs_review_count)` per fixture. The new save floor reflects "save all" semantics: the user receives at least everything the partition would have stored. A drop in count is a regression.
2. **Latency parity (SC-005)**: Same fixture, measure per-input wall time at the synchronous-return boundary, take p95 across the set. Capture before-cutover p95 into `baselines/latency_ms.json`. Test asserts post-cutover p95 ≤ baseline + 5% noise margin.
3. **HTTP response field parity (SC-006)**: Capture serialized JSON responses for the same fixture before cutover, store under `baselines/response_envelopes/<fixture>.json`. Test diffs post-cutover responses against baselines at the field-set level (presence of keys + value types), allowing the documented changes in `contracts/http-response-parity.md`: `place_type → categories`, `attributes → tags`, and the dropped emission of `status="needs_review"` (saved-bucket places now emit `status="saved"`; previously-needs_review places also emit `status="saved"` under ADR-071).
4. **Category vocabulary check** (not in the spec's SC list — log-only assertion): every picked place produces a non-empty `categories` list and every value is a valid `PlaceCategory` enum member.

**Rationale**: SC-005/006 stay gates. SC-007 changes from strict count parity to a floor assertion because ADR-071 deliberately raises save count (no more dropping needs_review). Without baselines captured at cutover time, these gates are unmeasurable.

**Alternatives considered**: Use production traffic replay — rejected, this repo doesn't have a replay harness; offline fixtures are sufficient. Keep strict-equality on partition counts under a "legacy flow" toggle — rejected, ADR-071 is a clean-cut decision.

---

## R-10 — Cutover rollout

**Decision**: Rolling deploy on Railway. Previous version drains in-flight requests (writes finish against legacy as they would have under the old code); new version handles requests that arrive after the swap. This is the standard Railway deploy behavior; no special handling required. Spec clarification Q1 = Option C (recorded in spec's `## Clarifications`).

**Rationale**: Already the default behavior; no in-app coordination needed. SC-002/003/004 are measured against post-drain steady state.

**Alternatives considered**: Hard cutover (drop all in-flight requests) — rejected as a deploy operation; we ship the code change, the platform handles in-flight requests as it always does.

---

## R-11 — Configuration changes

**Decision**: None to code-relevant config. `config/app.yaml`:
- `extraction.thresholds.save_threshold` / `confident_threshold` — **no longer load-bearing** after ADR-071. The partition logic that read them is gone. Keys remain in `app.yaml` for now to avoid a config diff in this feature, but they are dead config. A future feature may remove them.
- `extraction.cache_ttl_days: 30` — was a legacy-cache TTL; now ignored because v2 cache TTL is set in `places_v2/protocols.py:PLACE_CACHE_TTL_SECONDS`. Leave the key in `app.yaml` for now (legacy module still reads it for legacy-cache writes from non-extraction callers, until read-migration retires the legacy module).
- `models.extractor` / `models.embedder` / `models.vision_frames` / `models.transcriber` — unchanged.

**Rationale**: Provider abstraction (constitution principle III) is preserved without changes. v2's cache TTL is hard-coded in its protocol constants — out of scope for this feature to make it configurable.

**Alternatives considered**: Remove the dead `extraction.thresholds` keys in this feature — rejected, it's a config diff orthogonal to the code migration and risks producing a noisier review.

---

## R-12 — Test rewrite / delete plan

**Decision**:

| Existing test file | Action |
|---|---|
| `tests/core/extraction/test_searcher.py` | **DELETE** (searcher module is deleted). |
| `tests/core/extraction/test_places_client.py` | **DELETE** (legacy `PlacesClient` no longer wired from extraction; v2 client lives under `places_v2/google_client.py`, tested separately in `tests/core/places_v2/`). |
| `tests/core/extraction/test_dedup.py` | **REWRITE** — keep, target the slimmed-down `dedup_by_provider_id`. |
| `tests/core/extraction/test_persistence.py` | **DELETE** — `core/extraction/persistence.py` is deleted (ADR-071). Behavioral coverage moves into `test_service.py` (extraction flow now calls `upsert_and_embed` + `save_places` + dispatch inline). |
| `tests/core/extraction/test_service.py` | **REWRITE** — same flow, v2 stubs. |
| `tests/core/extraction/test_extraction_pipeline.py` | **REWRITE** — new geo-filter step replaces the legacy searcher hook. |
| `tests/core/extraction/test_url_source.py` | **REWRITE** (small) — `PlaceSource` import path changes. |
| `tests/core/extraction/test_types.py` | **REWRITE** — drop tests for `SearchMatch`; cover the new `ValidatedCandidate` shape. |
| `tests/core/extraction/enrichers/test_llm_picker.py` | **REWRITE** — new picker output schema. |
| `tests/core/extraction/enrichers/test_google_maps_list.py` | **REWRITE** (small) — `PlaceSource` import path. |
| Other enricher tests | **REWRITE if they touch `PlaceSource`** (subtitle_check, whisper_audio, vision_frames, vision_images, tiktok_*, video_metadata, instagram_post). Otherwise no change. |
| `tests/core/extraction/test_circuit_breaker.py`, `test_confidence_new.py`, `test_enrichment_level.py`, `test_input_parser.py`, `test_status_repository.py` | **NO CHANGE** (no place-vocabulary contact). |
| `tests/core/extraction/test_v2_cutover_parity.py` | **NEW** — partition / latency / response-shape parity (per R-09). |

**Rationale**: One row per file makes the test diff scope explicit.

**Alternatives considered**: Mass-rename via codemod — possible but unnecessary; each file's intent should be re-read by hand since the test scope itself changes.

---

## R-13 — API and agent surface changes

**Decision**:

- `api/schemas/extract_place.py`: `ExtractPlaceItem.place: PlaceObject` switches to v2 `PlaceObject`. Externally observed JSON field set is preserved by Pydantic field aliasing where any v2 field name differs from legacy — verified via the response-shape baseline (R-09 step 3).
- `api/routes/extraction.py`: dependency wiring shifts to v2 services (`PlacesSearchService`, `PlaceUpsertService`, `EmbeddingService`). No request schema change.
- `api/deps.py`: extraction-side dependency factories now return v2 services. Recall/consult/user deps still wire legacy services — untouched.
- `core/agent/tools/save_tool.py`: return type aligns to v2 `PlaceObject`. The agent's `state.last_recall_results` field (legacy `PlaceObject`) is *not* touched — that's the recall path and out of scope.

**Rationale**: FR-007 + FR-011 + FR-013. Save tool is the only agent tool in scope.

**Alternatives considered**: None.

---

## R-14 — Open items deferred to tasks

These do not block the plan but will surface during `/speckit.tasks`:

- Exact set of fixtures for R-09 — to be enumerated in `tasks.md`.
- Whether any enricher's `text_evidence` shape depends on legacy `PlaceSource` (none do based on `types.py:200` — `source` is on `ExtractionContext`, not on `Evidence`), but to be confirmed during implementation.
- Whether `core/agent/tools/save_tool.py` formats the return for the SSE stream differently — to be confirmed; if so, the formatting code lives agent-side and is in-scope.
