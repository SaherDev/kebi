# Feature Specification: PlacesService — Shared Data Layer for Place Storage and Enrichment

**Feature Branch**: `019-places-service`
**Created**: 2026-04-14
**Status**: Draft
**Input**: User description: Build PlacesService — a standalone shared data layer for save/recall/consult agent tools, with three storage tiers (permanent place store, geo cache, enrichment cache), a unified PlaceObject return type, and provider-namespaced external IDs.

## Clarifications

### Session 2026-04-14

- Q: Does the data layer enforce per-user ownership on reads, or does it trust upstream callers? → A: Trust upstream — reads take only place identifiers; the route and agent tool layer above the data layer are responsible for authorization before calling in. The data layer still stamps `user_id` on writes, but does not filter reads by user.
- Q: How does the data layer behave when the cache backend is unreachable or errors during enrichment? → A: Mixed degradation. Cache READ errors (geo MGET / enrichment MGET raising) degrade gracefully: the data layer treats every place in the request as a cache miss for the affected tier and continues. In location-only mode that means returning all places with `geo_fresh=False`. In full-enrichment mode that means routing all (or all-of-the-affected-tier) places to the provider fetch path, still subject to the per-request fetch cap. Cache WRITE errors (set_batch raising on writeback) are logged but swallowed — the call still returns successfully with the freshly fetched data, the cache just stays cold for next time. Permanent-store (database) errors remain fatal — they are not subject to graceful degradation.
- Q: What does `create()` do when a caller submits a place whose provider-namespaced identifier already exists in the permanent store? → A: Strict failure. `create()` raises a typed `DuplicatePlaceError` whose payload includes the existing place's internal `place_id`. It does NOT upsert and does NOT return the existing place silently. Callers that want idempotency must call the duplicate-detection lookup first (FR-004) and decide whether to skip, surface, or merge.
- Q: What happens in `create_batch` if one row in the batch collides on the provider-identifier unique constraint while the others would be fine? → A: All-or-nothing. The entire batch is rolled back in a single transaction and the data layer raises `DuplicatePlaceError` listing the conflicting provider identifier(s) (and, where available, the existing internal `place_id`s they map to). No partial inserts. The caller is expected to either pre-filter via duplicate-detection lookup or catch the error and retry with the conflicting rows excluded.
- Q: When an `enrich_batch` input contains the same `provider_id` more than once, does the data layer dedupe before cache reads and provider fetches? → A: Yes — dedupe internally by `provider_id`. The data layer collects the unique set of provider identifiers from the input, issues one cache MGET per tier across that unique set, fetches each missing key from the external provider exactly once, and then fans the merged data back out to every input position that referenced the key. Duplicates therefore consume one cache slot and one fetch-cap slot, not N. Input order is still preserved on the output, and every occurrence of a duplicated place is returned with the same enrichment data attached.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Save a Place with Provider Identity (Priority: P1)

A downstream agent tool (the "save" tool) hands the data layer a freshly extracted place — its name, type, attributes, source URL, and an external provider identifier (e.g. a Google Places ID) — and gets back a stable, persisted place record that can be referenced and enriched later.

**Why this priority**: Without persistent place storage, none of the other agent tools have anything to read, recall, or rank. Saving is the foundation that unlocks every other journey in the product.

**Independent Test**: A caller submits a place payload to the data layer and receives a unified place record with a stable ID. Submitting a second payload referencing the same external provider ID can be detected as a duplicate via lookup. No enrichment data needs to be present yet for this test to succeed.

**Acceptance Scenarios**:

1. **Given** a caller has extracted a place with a provider identifier, **When** the caller submits it to the data layer, **Then** the data layer persists the place permanently and returns a unified place record containing the caller-supplied content plus a stable internal ID.
2. **Given** a place with the same provider identifier already exists, **When** the caller asks the data layer to look it up by provider and external ID, **Then** the data layer returns the existing record so the caller can avoid creating a duplicate.
3. **Given** a caller submits a place with no provider identifier, **When** the data layer persists it, **Then** the place is stored without a provider reference and is excluded from any provider-keyed lookups or caches.
4. **Given** a caller submits multiple places at once (e.g. several places extracted from one source), **When** the data layer persists them, **Then** all places are written together and returned in the same order they were submitted.

---

### User Story 2 - Recall Saved Places with Location Context (Priority: P1)

A downstream agent tool (the "recall" tool) needs to surface a set of previously saved places to the user along with where each one is located, but does not need fresh hours, ratings, or other live details. The data layer must return location data fast and must never trigger external provider calls when serving recall.

**Why this priority**: Recall is the dominant read path. It is called for browsing, listing, map views, and any "what have I saved" experience. It must be fast and cheap, and it cannot be blocked or rate-limited by external providers.

**Independent Test**: A caller hands the data layer a list of saved place records and asks for location context only. The data layer returns the same list with coordinates and address attached for any place whose location is currently cached, and clearly marks which places had a fresh location and which did not. No external provider calls are made.

**Acceptance Scenarios**:

1. **Given** a caller has a list of saved places and asks for location-only enrichment, **When** the data layer processes the list, **Then** the data layer attaches cached location data (coordinates and address) to every place that has a current cache entry, and marks each place with a clear freshness indicator.
2. **Given** some places in the list have no cached location, **When** the data layer processes the list, **Then** those places are returned without location data, marked as not fresh, and no external provider call is triggered for them.
3. **Given** a place in the list has no provider identifier, **When** the data layer processes the list, **Then** the place passes through unchanged with no cache lookup attempted.
4. **Given** the caller submits a list in a specific order, **When** the data layer returns the enriched list, **Then** the order is preserved exactly.

---

### User Story 3 - Consult with Live Place Details (Priority: P1)

A downstream agent tool (the "consult" tool) is composing a single confident recommendation and needs full live details for a small candidate set: location, opening hours, current rating, phone, photo, popularity. The data layer must serve cached details where possible and fetch only the missing ones from the external provider.

**Why this priority**: Consult is the moment-of-truth read path that produces the user-facing recommendation. Stale or missing details degrade the recommendation directly. But fetching live details for every candidate is expensive and slow, so the data layer must cache aggressively and only call out for the gaps.

**Independent Test**: A caller hands the data layer a small list of candidate places and asks for full enrichment. The data layer returns the same list with location and live details attached for every place, fetching from the external provider only for places that were not already cached, and writing fresh fetches back into the cache for next time.

**Acceptance Scenarios**:

1. **Given** all candidate places already have cached location and cached live details, **When** the caller asks for full enrichment, **Then** the data layer returns the enriched list without making any external provider call.
2. **Given** some candidate places are missing cached data, **When** the caller asks for full enrichment, **Then** the data layer fetches details from the external provider only for the missing places, attaches the new data to the result, and stores the new data in both caches for future requests.
3. **Given** the data layer fetches new details, **When** the fetched details include opening hours, **Then** the hours include a time-zone identifier so callers can correctly interpret day boundaries for places in any region.
4. **Given** the number of missing places exceeds the configured per-request fetch limit, **When** the caller asks for full enrichment, **Then** the data layer fetches up to the limit, logs that a portion was dropped, and still returns every input place in order with whatever data is available.
5. **Given** a candidate place has no provider identifier, **When** the caller asks for full enrichment, **Then** the place passes through with no cache lookup and no fetch attempted.

---

### User Story 4 - Migrate Existing Place Records Without Data Loss (Priority: P2)

The system already has place records in the permanent store from earlier work, including columns that are about to be removed (cuisine, price range, location coordinates, etc.). The migration to the new place model must move that data into its new home before the old columns disappear, so no information is lost when the schema changes.

**Why this priority**: Existing data must survive the schema change. If the migration drops columns before relocating their content, the system loses information that took prior work to extract. This is one-time but irreversible if done wrong.

**Independent Test**: An operator runs the data-relocation script on a database that has rows with cuisine, price range, and location columns populated. After the script runs, the relocated data is present in its new home (within the structured attributes field for cuisine and price; within the location cache for coordinates). The schema migration then runs and drops the old columns, and no place loses its identity, name, or relocated data.

**Acceptance Scenarios**:

1. **Given** a place row has a cuisine value in the old column, **When** the relocation script runs, **Then** the cuisine value appears inside the structured attributes field on the new schema.
2. **Given** a place row has a price-range value in the old column, **When** the relocation script runs, **Then** the value is mapped into the structured attributes field using the standard price hint vocabulary (low → cheap, mid → moderate, high → expensive).
3. **Given** a place row has coordinates and an address along with a provider identifier, **When** the relocation script runs, **Then** that location data is seeded into the location cache under the provider-namespaced key with the standard cache lifetime, before the old columns are dropped.
4. **Given** the relocation script has run successfully, **When** the schema migration is applied, **Then** the old columns are removed and no rows lose their permanent identity (internal ID, name, type, source).

---

### Edge Cases

- A caller submits an empty batch of places to save: the data layer must return an empty result without touching the database.
- A place exists in the permanent store but has never been enriched: recall returns it with location absent and clearly marked not fresh; consult will fetch it on the next call.
- A cached location entry expires between two calls: the next consult call detects the miss and re-fetches; the next recall call returns the place without location and marked not fresh.
- A cached enrichment entry expires while the cached location is still valid: consult fetches new live details for that place but does not need to re-fetch the location.
- A provider call fails for one of several missing places in a batch: the data layer returns the rest of the batch with whatever data is available; the failed place passes through without enrichment and remains a candidate for retry on the next call.
- The location cache or enrichment cache is unreachable during a read: the data layer treats every place as a miss for that tier and continues — recall returns places with `geo_fresh=False`; consult routes the affected places into the provider fetch path (subject to the fetch cap). The caller does not see an error from the cache outage.
- The cache writeback fails after the data layer fetches fresh details from the provider: the failure is logged, the call still returns successfully with the freshly fetched data, and the cache stays cold for the next request.
- The permanent store is unreachable: the data layer raises and the caller sees an error. Permanent-store outages are not gracefully degraded.
- A place's provider identifier collides with one already stored: the data layer raises `DuplicatePlaceError` exposing the existing internal `place_id`. No upsert, no silent return-existing. The caller decides what to do (skip, surface, or merge by hand).
- A caller asks for full enrichment on a list where every place lacks a provider identifier: the data layer returns the list unchanged with no cache hits, no fetches, and no errors.
- A consult batch contains the same place twice: the data layer dedupes by provider identifier internally — exactly one cache lookup and at most one provider fetch is issued for that place — and each occurrence in the input is returned in its original position with the same enrichment data attached. Duplicates do not consume the per-request fetch cap more than once.
- A `create_batch` of N places includes one row whose provider identifier already exists: the entire batch is rolled back and `DuplicatePlaceError` is raised with the conflicting identifier(s). The other N-1 rows are NOT inserted. The caller can re-issue the batch with the conflicting row(s) removed.

## Requirements *(mandatory)*

### Functional Requirements

#### Permanent Store

- **FR-001**: The data layer MUST persist every place that callers create with a stable internal identifier, the place name, the place type, optional subcategory, optional descriptive tags, structured attributes, optional source URL, optional source platform, and an optional provider-namespaced external identifier.
- **FR-002**: The data layer MUST own the construction of the provider-namespaced external identifier from a plain external ID and a provider enum supplied by the caller. Callers MUST NOT construct or parse the namespaced string.
- **FR-003**: When either the provider or the plain external ID is absent on creation, the data layer MUST persist the place with no provider identifier and exclude it from provider-keyed operations.
- **FR-004**: The data layer MUST allow callers to look up a place by its provider and plain external ID for duplicate detection.
- **FR-005**: The permanent store MUST enforce uniqueness of the provider-namespaced external identifier, allowing many places with no identifier but at most one place per provider identifier.
- **FR-005a**: When a `create()` call would violate the provider-identifier uniqueness constraint (i.e. the same provider-namespaced identifier already exists), the data layer MUST raise a typed `DuplicatePlaceError` whose payload includes the existing place's internal `place_id`. The data layer MUST NOT silently upsert, merge, or return the existing place from `create()`. Callers wanting idempotent behavior are expected to use the duplicate-detection lookup (FR-004) first and decide explicitly.
- **FR-006**: The data layer MUST support batch creation of places in a single operation that preserves input order and returns an empty result when the input batch is empty without touching storage.
- **FR-006a**: `create_batch` MUST run inside a single transaction. If any row in the batch would violate the provider-identifier uniqueness constraint, the entire batch MUST be rolled back and the data layer MUST raise `DuplicatePlaceError` listing the conflicting provider identifier(s) (and, where available, the existing `place_id`s they map to). Partial inserts are not permitted.
- **FR-007**: The data layer MUST support fetching a single place or a batch of places by internal identifier, returning permanent-store fields only. Read operations do NOT take a user identifier and do NOT filter by ownership; the data layer trusts that the calling route or agent tool has already authorized the request.
- **FR-007a**: Write operations (`create`, `create_batch`) MUST stamp the caller-supplied `user_id` on every persisted row so ownership is recorded, even though it is not enforced on reads.
- **FR-008**: The permanent store MUST hold no data sourced from the external place provider beyond the namespaced external identifier itself (no live coordinates, address, hours, rating, phone, photo, popularity, or validation timestamp).
- **FR-009**: The permanent store MUST support efficient text search across place name and subcategory.
- **FR-010**: The permanent store MUST support efficient retrieval of all places belonging to one user, and of all places belonging to one user filtered by place type.

#### Place Vocabulary

- **FR-011**: Every persisted place MUST have a place type from the set: food and drink, things to do, shopping, services, accommodation.
- **FR-012**: A persisted place MAY have a subcategory drawn from the standard subcategory vocabulary for its place type.
- **FR-013**: A persisted place MAY have any number of descriptive tags drawn from the standard tag vocabulary (e.g. date-night, hidden-gem, queue-worthy, outdoor-seating, rooftop, etc.).
- **FR-014**: A persisted place's structured attributes MUST allow optional cuisine, optional price hint, optional ambiance, an optional list of dietary attributes, an optional list of "good for" attributes, and an optional location context (neighborhood, city, country) extracted from the source content rather than the external provider.
- **FR-015**: The price hint vocabulary MUST be exactly: cheap, moderate, expensive, luxury.

#### Location Cache (Tier 2)

- **FR-016**: The data layer MUST maintain a location cache, keyed by the provider-namespaced identifier, holding latitude, longitude, address, and a cached-at timestamp.
- **FR-017**: Location cache entries MUST expire after a configurable lifetime (default thirty days) without any cleanup job; expiry happens automatically by cache TTL.
- **FR-018**: The data layer MUST read the location cache only in batch (one round trip per request, regardless of batch size). Single-key loops are prohibited.
- **FR-019**: The data layer MUST write the location cache only in batch (one round trip per write set).

#### Enrichment Cache (Tier 3)

- **FR-020**: The data layer MUST maintain an enrichment cache, keyed by the provider-namespaced identifier, holding opening hours (with time zone), rating, phone, photo URL, popularity score, and a fetched-at timestamp.
- **FR-021**: Enrichment cache entries MUST expire after a configurable lifetime (default four hours) without any cleanup job.
- **FR-022**: The data layer MUST read and write the enrichment cache only in batch.
- **FR-023**: Opening hours stored in the enrichment cache MUST include a time zone identifier whenever any day data is present, so day boundaries can be interpreted unambiguously across regions.

#### Unified Return Type and Freshness Indicators

- **FR-024**: Every read or write operation in the data layer MUST return places using one unified shape that exposes permanent-store fields, optional location fields, optional live-detail fields, and two boolean freshness indicators: one for whether location came from the cache this call, and one for whether live details were populated this call.
- **FR-025**: A newly created place MUST be returned with both freshness indicators set to false.
- **FR-026**: A place fetched by internal identifier MUST be returned with both freshness indicators set to false.

#### Cache Backend Failure Behavior

- **FR-026a**: When the location cache or enrichment cache READ operation (batch get) fails or is unreachable, the data layer MUST treat every place in the request as a cache miss for the affected tier and continue. In location-only mode this means returning every place with `geo_fresh=False` and no error raised to the caller. In full-enrichment mode this means routing the affected places into the provider fetch path (still subject to the per-request fetch cap defined in FR-030).
- **FR-026b**: When the location cache or enrichment cache WRITE operation (batch set / writeback) fails, the data layer MUST log the failure and return successfully with the data it just fetched. The cache simply stays cold for the next request. Write failures MUST NOT cause an enrichment call to error out for the caller.
- **FR-026c**: Permanent-store (database) errors are NOT subject to graceful degradation. A failure to read from or write to the permanent store MUST surface as an error to the caller. Cache fallback applies only to the cache tiers, never to the system of record.

#### Enrichment Workflows

- **FR-027**: The data layer MUST offer an enrichment operation in two modes: a "location only" mode for the recall use case, and a "full enrichment" mode for the consult use case.
- **FR-028**: In location-only mode, the data layer MUST consult the location cache only, MUST NOT consult the enrichment cache, and MUST NOT make any external provider call regardless of cache misses. Hits set the location-fresh indicator; misses leave it false.
- **FR-029**: In full-enrichment mode, the data layer MUST consult both caches in a single round trip each, identify the union of places missing data, fetch those missing places from the external provider in parallel, write the new data into both caches, and merge all data onto the returned places.
- **FR-029a**: Before issuing cache reads or provider fetches, the data layer MUST dedupe by provider-namespaced identifier. The cache MGETs and the provider fetches MUST operate on the unique set of identifiers in the request, not on the full input list. The fetch cap (FR-030) MUST count unique identifiers, not input positions. After fetching, the merged data for each unique identifier MUST be fanned out to every input position that referenced it, so duplicate occurrences in the input are all returned with the same enrichment attached, in their original positions, and in input order.
- **FR-030**: In full-enrichment mode, the data layer MUST cap the number of external provider fetches per request at a configurable limit (default ten). When the cap is exceeded, the data layer MUST drop the overflow, log a warning containing the dropped count, and still return every input place in order.
- **FR-031**: In any enrichment mode, places lacking a provider-namespaced identifier MUST pass through unchanged with no cache lookup, no external fetch, and no error.
- **FR-032**: In any enrichment mode, the data layer MUST return places in the same order they were supplied.
- **FR-033**: The data layer MUST strip the provider namespace from the identifier before passing it to the external provider; external providers never see the namespaced form.

#### Migration of Existing Data

- **FR-034**: Before the schema change drops legacy columns, the system MUST relocate existing data so nothing is lost: legacy cuisine values move into the structured attributes field as cuisine; legacy price-range values move into the structured attributes field as price hint using the mapping low → cheap, mid → moderate, high → expensive; rows that have coordinates, an address, and a provider identifier seed the location cache under the namespaced key with the standard cache lifetime.
- **FR-035**: The schema migration file MUST document, in a comment, that the relocation step must be run before applying the schema change.
- **FR-036**: After relocation and the schema change, every legacy place row MUST retain its internal identifier, name, type, source URL, and source attribution.

#### Configuration

- **FR-037**: The location cache lifetime, the enrichment cache lifetime, and the per-request external-fetch cap MUST all be configurable via the project's central configuration file. No cache lifetime or limit may be hardcoded.

### Key Entities

- **Place (Permanent Record)**: A unique place known to the system. Holds the stable internal identifier, the user it belongs to, the place name, the place type, optional subcategory, descriptive tags, structured attributes (cuisine, price hint, ambiance, dietary, good-for, location context), the source URL and source platform the place came from, and an optional provider-namespaced external identifier. Holds no data sourced from the external place provider beyond the namespaced identifier.
- **Place Attributes**: A structured bag of qualitative and contextual descriptors for a place: cuisine, price hint, ambiance, dietary list, good-for list, and location context (neighborhood, city, country).
- **Location Cache Entry**: A cached snapshot of a place's location: latitude, longitude, address, and a cached-at timestamp. Lives in the location cache under the provider-namespaced key. Expires after the configured lifetime.
- **Enrichment Cache Entry**: A cached snapshot of a place's live details: opening hours (with time zone), rating, phone, photo URL, popularity, and a fetched-at timestamp. Lives in the enrichment cache under the provider-namespaced key. Expires after the configured lifetime.
- **Place Object (Unified Return Type)**: The shape every data-layer operation returns. Combines permanent-store fields, optional location-cache fields, optional enrichment-cache fields, and two boolean freshness indicators describing what was populated on this call.
- **Provider**: The external place authority that issued an external identifier (e.g. Google, Foursquare, manual). Combined with a plain external ID, becomes the provider-namespaced identifier owned by the data layer.
- **Place Type and Subcategory Vocabulary**: A closed set of place types (food and drink, things to do, shopping, services, accommodation) each with its own closed set of subcategories. Stored as plain strings on the permanent record.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A recall request for a list of saved places returns location data without making a single external provider call, in any scenario, regardless of which places are cached.
- **SC-002**: A consult request for a list of candidate places where every place is already cached returns full live details without making a single external provider call.
- **SC-003**: A consult request that needs N missing places to be fetched issues exactly one external provider call per missing place, capped at the configured per-request limit, with the calls running in parallel rather than sequentially.
- **SC-004**: Any read or write operation on either cache uses exactly one cache round trip regardless of how many places are in the batch.
- **SC-005**: After the schema migration runs, the permanent store contains zero columns sourced from the external place provider beyond the namespaced external identifier.
- **SC-006**: After the data relocation step runs against a populated database, every legacy cuisine value, every legacy price-range value, and every legacy location-with-provider row is reachable through the new schema or the location cache; no relocated value is silently lost.
- **SC-007**: Any place lacking a provider identifier flows through every enrichment mode without triggering a cache lookup or a provider call, and is returned in its original input position.
- **SC-008**: Returned place lists in every batch operation match the input order exactly, with no reordering, deduplication, or omission.
- **SC-009**: Cache lifetimes (location and enrichment) and the per-request external-fetch cap can be changed by editing one configuration file, with no code change required.
- **SC-010**: A duplicate-creation attempt using the same provider identifier is detectable by the caller via lookup before insertion. If the caller skips the lookup and attempts the write, `create()` raises `DuplicatePlaceError` with the existing `place_id` attached so the caller can recover without a second round trip.

## Assumptions

- **Three agent tools (save, recall, consult) are the only callers** of the data layer. The data layer is built standalone in this feature; wiring those tools to it happens in later features.
- **The external place provider is a Google-Places-equivalent service**: it can validate a place by name and rough location, fetch full details by an external ID, and discover nearby places by category and radius. The data layer is provider-agnostic at the contract level; only the concrete client implementation is provider-specific.
- **The location cache and the enrichment cache live in the same key-value store** that already serves the rest of this repo, namespaced by key prefix (`places:geo:` and `places:enrichment:`).
- **The permanent store is the existing project database**. Schema ownership and migration tooling already exist for this repo.
- **A "user" already exists** as a foreign-key target in the permanent store. This feature does not create or own the user concept.
- **Authorization is upstream**: the route layer and agent tools that call the data layer are responsible for verifying that the requesting user is allowed to read or write the place(s) in question. The data layer is a trusted internal component and does not re-check ownership on reads.
- **Cache TTL is sufficient for cleanup**: there is no background job, no cron, and no manual eviction. Stale entries simply expire on their own.
- **Default tunables**: location cache thirty days, enrichment cache four hours, per-request external-fetch cap ten places. All three are overridable in configuration.
- **Time zones**: opening hours are stored with an IANA time zone identifier whenever any day data is present, so callers in any region can interpret day boundaries correctly. A null day value means closed; a missing day key means unknown.
- **The price-range mapping for the legacy data relocation is fixed**: low → cheap, mid → moderate, high → expensive. Any other legacy values are left out of the structured attributes field rather than being guessed.
- **Recall never populates the live-details freshness indicator**: even if hot enrichment data happens to exist, recall does not surface it. The two enrichment modes are strictly separated by intent.

## Out of Scope

- Wiring the data layer into the save, recall, or consult tools. Those tools are not modified in this feature.
- Modifying ExtractionService, RecallService, ConsultService, or any existing route or agent code.
- Any work in the product repo (NestJS / `kebi-app`).
- Ranking, taste-model integration, or recommendation scoring.
- A background job, cron, or manual sweeper to evict cache entries — TTL handles all expiry.
- Re-fetching live details on a fixed schedule outside of consult requests.
- Multi-provider routing logic (e.g. trying Foursquare when Google has no result). The provider abstraction supports it, but only one concrete provider is delivered in this feature.
- Geographic search by radius from a user's current location (covered by the existing nearby-discovery primitive on the provider client; not exposed as a data-layer operation in this feature).
