# Feature Specification: Extraction → Places v2 Cutover

**Feature Branch**: `030-places-v2-migration`
**Created**: 2026-05-13
**Status**: Draft
**Input**: User description: "the idea is to change everything to places_v2, so if we have any model cache or db or model or what ever we need to drop it and use only the new places_v2 compnennt" — narrowed by follow-up: "this task is about the extraction flow only"

## Summary

The place-extraction flow (request parsing → enrichment → search → LLM picker → persistence → embedding → response envelope) currently writes to the legacy place store: legacy place table, legacy embeddings table, legacy place cache namespaces, and the legacy place object shape. The v2 place module already exists alongside (`places_v2`, `user_places`, `place_embeddings_v2`, v2 cache, v2 place object, v2 vocabulary). This feature cuts the extraction flow over to v2 only: every place row, every embedding, every geo cache entry, and every externally returned place produced by extraction goes to v2. Read-side features (recall, consult, agent recall tool, taste model, user-places API) are explicitly out of scope and continue to use the legacy store during this feature; they migrate in follow-up work.

## Clarifications

### Session 2026-05-13

- Q: At cutover deploy, how do in-flight extractions behave? → A: Drain on the previous version (standard rolling-deploy semantics). In-flight requests finish on the old code, writing to legacy as they would have; the new code only handles requests that arrive after the swap. SC-002/SC-003/SC-004 (zero legacy writes in a 24h window) are measured against post-drain steady state, not the brief drain interval.
- Q: Is the v2 module modifiable as part of this feature? → A: No. The `places_v2` module is treated as frozen for this feature. All changes happen on the extraction side: extraction code adapts to what `places_v2` already exposes (types, services, repositories, cache, embedding store, vocabulary). Where extraction has a legacy attribute or behavior with no exact v2 equivalent, the resolution lives inside the extraction flow (map, transform, or drop) — it does not extend or modify v2.
- Q: How does the extraction search step land on `places_v2`? → A: Per ADR-070, delete `core/extraction/searcher.py` (`PlacesSearcher`, `SearchMatch`) and delegate name → candidate-set lookup to `places_v2.PlacesSearchService.find()`. The service handles DB read, cache overlay, provider fallback, and upsert. Geographic-feature filtering (locality, country, route) stays inside extraction as a thin filter over the service's result. `core/extraction/types.py` drops every re-export from `core.places`; `ValidatedCandidate` is reshaped against v2 vocabulary (`PlaceCategory` / `PlaceTag` / v2 `PlaceSource` / namespaced `provider_id`); pipeline-state types (`Producer`, `Medium`, `Evidence`, `KnownPlace`, `ExtractionContext`) stay.
- Q: How does extraction persist saved places? → A: Per ADR-071 (supersedes ADR-057), drop `ExtractionPersistenceService` entirely. Confidence partitioning at save time is deprecated. Extraction inlines two v2 service calls: `PlaceUpsertService.upsert_and_embed(cores)` (DB upsert + embedding in one call) followed by `UserPlacesService.save_places(user_id, cores, source, source_url)` (links to `user_places` with `approved=False`). Duplicate handling: extraction pre-filters via `UserPlacesRepo.get_existing_place_ids` so the batch sent to `save_places` is conflict-free; the duplicate ids surface in the response as `status="duplicate"`. Save everything the picker emits — the user curates after. `ExtractPlaceItem.status` literal stays `"saved" | "needs_review" | "duplicate"` for backward compat but extraction never emits `"needs_review"` after this cutover.
- Q: Is `LLMPlacePicker` (the Instructor-typed LLM step) in scope for vocabulary alignment? → A: Yes, explicitly. The picker's Instructor output Pydantic model is rewritten to v2 vocabulary (research R-02): drops `place_type: PlaceType`, `provider: PlaceProvider`, `external_id: str`, `attributes: PlaceAttributes`; emits `categories: list[PlaceCategory]` (1–3 per pick), `tags: list[PlaceTag]` (each stamped `source="llm"`), `provider_id: str` (namespaced, echoed from the `PlacesSearchService.find()` result set). The prompt text is rewritten to describe v2 vocabulary — list of allowed `PlaceCategory` values, `TagType` axes with their enum value sets, instruction to choose by `provider_id`. `reconcile_picks` joins picker output to the search-service result set by `provider_id`. Confidence numeric field is unchanged (still computed in `core/extraction/confidence.py`) and is metadata only — not a save gate (per ADR-071).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Extraction persists saves only to the v2 store (Priority: P1)

When the system extracts a place from a user-shared URL or text, the resulting place row, geo cache entry, and embedding all land in the v2 store. The extraction flow never writes to the legacy place table, the legacy embeddings table, or any legacy place cache namespace.

**Why this priority**: This is the core deliverable. Without it, the extraction flow keeps producing legacy data and the migration goal is not met. With it shipped, all newly saved places are v2-native; the legacy store stops receiving fresh saves and freezes as historical data for the duration of the follow-up read migration.

**Independent Test**: Run an extraction request end-to-end through the unified entry point and through the polling endpoint. Inspect the database, the cache layer, and the embedding store. Confirm: (a) the new place row exists in the v2 table and not in the legacy table; (b) the embedding is recorded in the v2 embedding store with the current embedder model name; (c) cache entries appear under the v2 namespace, not legacy namespaces; (d) the polling response envelope still answers correctly.

**Acceptance Scenarios**:

1. **Given** a user shares a supported URL with one recognizable place, **When** extraction completes successfully, **Then** exactly one new row appears in the v2 place table and zero new rows appear in the legacy place table.
2. **Given** an extraction yields a duplicate provider id, **When** the duplicate is detected, **Then** the existing v2 row is merged per the current merge policy (sticky name, deduplicated aliases and tags, union of categories) and the legacy table is untouched.
3. **Given** a candidate scores between the save threshold and the confident threshold, **When** the partition runs, **Then** the row is stored in the v2 store with the needs-review status; below-threshold candidates are dropped; at-or-above-confident candidates are stored as saved. Partition counts on a fixed offline fixture set match the pre-migration baseline.
4. **Given** an extraction succeeds with at least one saved candidate, **When** the embedding step runs, **Then** the vector is upserted into the v2 embedding store keyed by the v2 place id and the embedder model name, and the legacy embedding store receives no write.
5. **Given** an extraction succeeds, **When** the geo data is cached, **Then** the cache entry is written under the v2 cache namespace and no entry is written under any legacy place cache namespace.

---

### User Story 2 - Extraction internals depend only on v2 types (Priority: P2)

Every file inside the extraction flow — the orchestrating service, the persistence layer, the search layer, the deduplication helper, the enrichers, the source-filter base, the URL-source classifier, and the extraction-internal type module — imports its place data shapes, source enums, category and tag vocabulary, provider-id helpers, and write services from the v2 module only. The legacy module is not imported anywhere in the extraction flow.

**Why this priority**: Story 1's behavioral outcome (writes land in v2) cannot be achieved without first replacing the legacy types and write services that the extraction flow currently imports. Story 2 is the structural work that makes Story 1 verifiable. It is broken out as its own story so it can be reviewed independently of behavior changes: a single PR or commit set that swaps imports and types, with no behavioral surprises beyond what Story 1 requires.

**Independent Test**: Grep across `core/extraction/` for any reference to the legacy place module — zero results. Type-check the extraction flow in isolation — passes. Existing extraction tests that did not assert on legacy storage internals continue to pass.

**Acceptance Scenarios**:

1. **Given** the extraction flow files are inspected, **When** a code search runs for legacy place module imports, **Then** the result is empty.
2. **Given** the v2 vocabulary covers a previously-legacy attribute or enum (source kind, provider, category, tag), **When** the extraction code uses that vocabulary, **Then** the v2 enum or model is the only source imported and the legacy equivalent is not aliased or re-exported.
3. **Given** a previously-legacy attribute or enum has no direct v2 equivalent, **When** the migration encounters it, **Then** the attribute is either mapped to the closest v2 representation or recorded as deprecated for this extraction flow, with the mapping or deprecation explicit in the diff.

---

### User Story 3 - Extraction's external response carries the v2 place shape (Priority: P3)

The extraction response envelope — what is returned synchronously from the extract endpoint, surfaced through the unified chat entry's save tool, and stored for the extraction status polling endpoint — embeds the v2 place shape. The externally observed field set on the response (field names, types, semantics) remains compatible with what callers see today.

**Why this priority**: Story 1 ensures saves land in v2; Story 3 ensures the externally returned place objects reflect that. Until Story 3 ships, external callers might receive legacy-shaped place objects even though the underlying row is v2 — a transient and confusing state. Once Story 3 ships, the extraction contract is internally consistent: v2 storage, v2 shape on the wire.

**Independent Test**: Run a representative fixture set of extraction requests, capture the JSON responses, and compare them at the field level against the pre-migration contract. Verify field names, types, and semantics are equivalent. Verify the polled status envelope returns the same shape as the synchronous response.

**Acceptance Scenarios**:

1. **Given** the extraction response schema, **When** it is inspected, **Then** the embedded place type is the v2 place object and not the legacy place object.
2. **Given** a saved extraction request id, **When** the status polling endpoint is called, **Then** the returned envelope contains v2-shaped place objects with the same externally visible field set as before.
3. **Given** an external integration that consumed the previous response shape, **When** it consumes the new response, **Then** all fields it relied on are present with equivalent semantics, or any divergence is explicitly listed as an externally communicated change.

---

### Edge Cases

- An extraction is mid-flight when the cutover deploys, with a status envelope already persisted under the polling key. Rolling-deploy semantics apply: the previous version drains its in-flight requests against the legacy store (because they began under the old code), and only requests that arrive after the version swap run against v2. Partial writes spanning legacy and v2 must not be possible for any single request.
- A previously-legacy attribute on the LLM picker output (provider tag, type tag, attribute set) has no exact v2 equivalent. The cutover must either map it to the closest v2 vocabulary or explicitly drop it; the spec requires the choice to be visible in the diff.
- Two simultaneous extractions race on the same provider id. Duplicate detection must continue to converge on a single v2 row.
- A place that was previously saved to the legacy store is extracted again post-cutover. Because the v2 store has its own provider-id namespace, the new save is treated as a new v2 row; this is an intentional consequence of the clean-cut policy and is not deduplicated across stores.
- Reads from recall, consult, agent recall, and the user-places API run while extraction is writing only to v2. The read side is intentionally not updated in this feature, so newly extracted places are not visible to those read paths until follow-up work ships. This is the explicitly accepted temporary state.
- A test fixture or factory in the extraction test tree still references the legacy module. The test suite must fail cleanly rather than silently skip.
- The polling endpoint stores response envelopes under a short-lived cache key. Envelopes serialized before the cutover may carry the legacy shape; envelopes serialized after carry the v2 shape. Consumers that poll across the cutover boundary must either fall through gracefully or be tolerated by the envelope TTL.

## Requirements *(mandatory)*

### Functional Requirements

**Writes**

- **FR-001**: The extraction flow MUST write new place rows only to the v2 place table. It MUST NOT write to the legacy place table.
- **FR-002**: The extraction flow MUST write embeddings only to the v2 embedding store. It MUST NOT write to the legacy embeddings table.
- **FR-003**: The extraction flow MUST write geo cache entries only under the v2 cache shape. It MUST NOT write under any legacy place cache namespace (geo, enrichment, geocode, or any other legacy prefix).
- **FR-004**: The extraction flow MUST save every candidate the picker emits as a `user_places` row with `approved=False`. Confidence-band partitioning at save time is deprecated per ADR-071 (supersedes ADR-057); the user curates after the fact via the user-places approve / delete surface. `ExtractPlaceItem.status` literal stays `"saved" | "needs_review" | "duplicate"` for backward compatibility but the extraction flow never emits `"needs_review"` — only `"saved"` or `"duplicate"`.
- **FR-005**: Duplicate detection MUST occur in two layers: (a) the v2 place-row layer — duplicates by `provider_id` merge into a single v2 row via the existing merge policy (sticky name, deduplicated aliases and tags, union of categories); (b) the user-place link layer — places already in the user's saved list are pre-filtered before linking, and duplicate ids appear in the response with `status="duplicate"`.
- **FR-006**: Embedding generation MUST continue to use the configured embedder role and continue to skip re-embedding when the text hash and model name match an existing row. The mechanic is `PlaceUpsertService.upsert_and_embed` — DB upsert and embed happen as one v2 service call.
- **FR-007**: The extraction flow MUST be capable of completing an extraction without any code path inside the flow importing the legacy place module.

**Internals**

- **FR-008**: Every file in the extraction flow (the orchestrating service, the persistence layer, the search layer, the dedup helper, the source-filter base, the URL-source classifier, the extraction-internal type module, and every enricher) MUST import its place data shapes, source enums, vocabulary (categories, tags), provider-id helpers, and write services from the v2 module only.
- **FR-009**: Where a v2 equivalent for a previously-legacy attribute or enum used by extraction exists, the v2 form MUST be used directly. The legacy form MUST NOT be aliased, re-exported, or wrapped to look like the legacy form.
- **FR-010**: Where a previously-legacy attribute or enum has no v2 equivalent, the migration MUST either map it to the closest v2 representation (with the mapping visible in the diff) or explicitly drop it from the extraction flow (with the deprecation visible in the diff). It MUST NOT silently retain the legacy form.
- **FR-010a**: No file under `core/places_v2/` MAY be modified by this feature. All adapters, mappers, transforms, or drop decisions required to bridge legacy concepts to v2 MUST live inside the extraction flow.

**External Response**

- **FR-011**: The extraction response schema, the unified chat entry's save tool return, and the extraction status polling envelope MUST embed the v2 place shape, not the legacy place shape.
- **FR-012**: The externally observed field set of the extraction response (field names, types, semantics) MUST remain compatible with the pre-migration contract. Any divergence MUST be explicit and documented in the diff, not incidental.

**Out-of-flow non-interference**

- **FR-013**: Code outside the extraction flow (recall, consult, agent recall and consult tools, user-places API, taste model, chat service, db repositories serving reads) MUST remain functional and unchanged. This feature does not migrate or remove them.
- **FR-014**: The legacy place module, the legacy place table, the legacy embeddings table, the legacy place cache namespaces, and any schema migration that defines them MUST remain in place after this feature. They are not deleted or dropped in this feature.

**Tests**

- **FR-015**: Tests under the extraction test tree MUST be updated to exercise the v2 write paths. Tests asserting on legacy storage internals must be rewritten or deleted; tests asserting on extraction behavior (input → output, response shape, success and failure codes) must continue to pass.
- **FR-016**: New regression coverage MUST exist for: extraction's persistence into the v2 place table, extraction's embedding into the v2 embedding store, extraction's cache write into the v2 namespace, and extraction's response envelope carrying the v2 shape.

**Observability**

- **FR-017**: Tracing, logging, and structured fields produced by the extraction flow MUST attribute place operations to v2 components after the cutover. Any dashboard, alert, or saved query that filters extraction-flow place operations on legacy module names is identified before the cutover.

### Key Entities *(include if feature involves data)*

- **Place (v2)**: The canonical real-world venue entity used by the extraction flow after this feature. Holds the identity (provider id), descriptive attributes (name, aliases, categories, tags, location), and lifecycle metadata (source, created timestamp).
- **Place embedding (v2)**: The vector representation of a place's descriptive text, keyed by v2 place id and embedder model name, written by the extraction flow's embedding step.
- **Place cache entry (v2)**: The short-lived externally sourced enrichment payload (geo, address, hours, rating, photos) keyed by provider id, written by the extraction flow under the v2 cache namespace.
- **Extraction response envelope**: The synchronous and pollable response document carrying the request status, saved place(s), and reason codes. After this feature, the embedded place objects are v2-shaped.
- **Legacy place store (read-only during this feature)**: The legacy place table, legacy embeddings table, and legacy cache namespaces continue to exist and serve read traffic from out-of-scope features. They receive no new writes from the extraction flow after the cutover.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: After the cutover, a code search across `core/extraction/` for any import from the legacy place module returns zero results.
- **SC-002**: During a representative load window of at least 24 hours after the cutover, the legacy place table receives zero new rows attributable to the extraction flow.
- **SC-003**: During the same load window, no cache entries are written under any legacy place cache namespace by the extraction flow.
- **SC-004**: During the same load window, no rows are written to the legacy embeddings table by the extraction flow.
- **SC-005**: End-to-end extraction latency at the 95th percentile, measured from extraction request to response envelope completion, is the same or better than the pre-migration baseline.
- **SC-006**: External responses from the unified chat entry, the extract endpoint, and the extraction status polling endpoint are field-compatible with the pre-migration contract on a representative fixture set.
- **SC-007**: For a fixed offline fixture set, the count of places saved per request after the cutover is ≥ the count saved by the pre-migration partition logic for the same input. Rationale: ADR-071 supersedes ADR-057 — the extraction flow no longer drops candidates by confidence band, so the post-cutover save count is the union of what would previously have been the `saved` and `needs_review` bands, minus any picker-layer differences. Net effect: the user receives at least as many saved candidates as before.
- **SC-008**: The full extraction test suite passes after the cutover with no references to the legacy module in extraction test files.
- **SC-009**: Lint and type-check pass for the extraction flow with no new warnings or errors attributable to this feature.

## Assumptions

- **Clean cut, not dual-write.** Per the user's explicit decision, extraction writes only to the v2 store after cutover. Extraction does not also write to the legacy store during a coexistence window.
- **Read-side regression is intentional and accepted.** Recall, consult, the agent recall tool, the user-places API, and the taste model continue to read from the legacy store. Until the follow-up read-migration feature ships, places newly saved by extraction in v2 are not visible to these read paths. This is the explicitly accepted temporary state and is not a defect of this feature.
- **Agent save tool is in scope; agent recall tool is not.** Save-tool return values become v2-shaped because they are part of the extraction flow's externally observed output. Recall-tool return values and agent state fields that hold previously recalled places remain legacy-shaped during this feature.
- **No cross-store deduplication.** A place previously saved to the legacy store and extracted again post-cutover is treated as a new v2 save. The system does not attempt to find the legacy row and migrate it.
- **v2 module is frozen for this feature.** The `places_v2` module already exposes a write service with merge policy, an embedding store with diff-then-embed, a geo cache, and source/category/tag vocabulary. All changes in this feature happen on the extraction side; no file under `core/places_v2/` is modified. Where extraction has a legacy concept with no exact v2 equivalent, the resolution (map, transform, or drop) lives inside the extraction flow.
- **External HTTP contract is preserved.** Internal type renames in extraction are expected and acceptable. Externally observable field names, types, and semantics on the response and polling envelopes are preserved.
- **Save-all model (ADR-071) supersedes confidence partitioning.** Every picker output is saved as a `user_places` row with `approved=False`; the user curates. Merge policy on the place row (sticky name, deduplicated aliases and tags, union of categories) is preserved unchanged.
- **Legacy module, tables, and cache namespaces survive this feature.** They are retired in the follow-up read-migration feature, not here. Schema-drop migrations are not in scope.

## Out of Scope

- Modifying any file under `core/places_v2/`. The v2 module is treated as frozen for this feature; extraction adapts to its current public surface.
- Removing the legacy place module, deleting its source files, or dropping any legacy schema migration files.
- Dropping the legacy place table, legacy embeddings table, or any other legacy database object.
- Decommissioning the legacy place cache namespaces.
- Migrating recall, consult, agent recall, taste model, the user-places API, or the chat service to read from the v2 store.
- Migrating the legacy place data into v2 (no backfill).
- Building a dual-write compatibility window. Cutover is clean.
- Adding new features to the v2 module beyond what extraction already needs.
- Retuning extraction embedding model selection or any other behavioral parameter. (Confidence thresholds are no longer load-bearing after ADR-071; the `config/app.yaml extraction.thresholds` keys are dead config until either repurposed or removed in a future feature.)
- Changing externally observed HTTP contracts.
