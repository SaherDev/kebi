# Feature Specification: Place Extraction Endpoint (Phase 2)

**Feature Branch**: `002-extract-place`
**Created**: 2026-03-24
**Status**: Draft

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Share a TikTok food video, get a saved place (Priority: P1)

A user pastes a TikTok URL into the Kebi app after watching a food video. The product system sends that URL to the extraction endpoint. The system reads the video caption, identifies the restaurant name and any available details, validates it against a places database, and saves a structured place record linked to the user. The product system receives confirmation that the place was saved — or a flag indicating the user should confirm the name before saving.

**Why this priority**: This is the primary Phase 2 use case. TikTok is the dominant source of food discovery content. Capturing places from TikTok URLs without requiring the user to manually type anything is the core value proposition.

**Independent Test**: Send a TikTok URL for a known restaurant to the endpoint. Verify a place record appears in the database with correct name, cuisine, and price range. Verify the response includes a confidence score and no `requires_confirmation` flag when the restaurant name is unambiguous.

**Acceptance Scenarios**:

1. **Given** a valid TikTok video URL whose caption mentions a restaurant by name, **When** the endpoint receives that URL with a valid user ID, **Then** the response contains a place name, the place is written to the database, and `requires_confirmation` is false.
2. **Given** a TikTok URL whose caption is empty or mentions no identifiable place, **When** the endpoint receives that URL, **Then** the response either returns an error indicating extraction failed or sets `requires_confirmation` to true with a low confidence score.
3. **Given** a TikTok URL for a restaurant that matches exactly in the places database, **When** extraction completes, **Then** the confidence score is at least 0.90 and the validated place name from the database is used in the response.

---

### User Story 2 - Share a place by typing its name or description (Priority: P2)

A user types the name of a restaurant they visited — e.g., "Fuji Ramen on Sukhumvit Soi 33, Bangkok" — into the app. The product system sends this plain text to the extraction endpoint. The system identifies the structured place data from the text and saves the record.

**Why this priority**: Plain text is the fallback for all cases where the user has no link to share. It must work reliably for the taste model to accumulate data from day one.

**Independent Test**: Send a plain text string with a restaurant name and city. Verify a place record is created with the correct name and that the confidence score reflects the match quality from the places database.

**Acceptance Scenarios**:

1. **Given** a plain text input containing a recognizable restaurant name and city, **When** the endpoint processes it, **Then** a place record is created and the response contains a confidence score ≥ 0.70, and `requires_confirmation` is false.
2. **Given** a vague plain text input with no identifiable place name ("somewhere good for ramen"), **When** the endpoint processes it, **Then** the response returns an extraction failure error rather than saving a speculative record.
3. **Given** plain text where the extracted name matches the places database only approximately, **When** the endpoint processes it, **Then** `requires_confirmation` is true, the response includes the best-guess place name, and no record is written to the database until the user confirms. The product system surfaces the candidate name to the user; if the user confirms or corrects it, the product system calls this endpoint again with the confirmed input, which then saves the record normally.

---

### User Story 3 - Low-confidence extraction prompts user confirmation (Priority: P3)

When the system cannot confidently identify the place — because the caption is ambiguous, the name is unusual, or no match is found in the places database — the response signals to the product system that human confirmation is required before the record is saved.

**Why this priority**: Without this gate, bad data enters the taste model and degrades recommendation quality. Accuracy is more important than throughput.

**Independent Test**: Send a TikTok URL or plain text with an ambiguous or obscure place name. Verify the response sets `requires_confirmation: true`, the confidence score is below 0.70, and no place record appears in the database.

**Acceptance Scenarios**:

1. **Given** any input where the extracted place name cannot be validated against the places database at all, **When** extraction completes, **Then** the response returns an extraction-failed error type and the product system can surface a prompt asking the user to type the place name manually.
2. **Given** any input where confidence is between 0.30 and 0.70, **When** extraction completes, **Then** the response includes the candidate place name and sets `requires_confirmation: true`. No DB record is written.
3. **Given** any input where confidence is at or below 0.30, **When** extraction completes, **Then** the system returns an error response indicating the place could not be identified, without saving any record.

---

### Edge Cases

- What happens when the input is a TikTok URL but the video has no caption? → Extraction returns a low or zero confidence result; response signals confirmation needed or returns extraction-failed error.
- What happens when the TikTok oEmbed service is unreachable or takes longer than 3 seconds? → The call times out after 3 seconds and the extraction step fails with a 500 error; no partial record is saved.
- What happens when the places validation service returns no match? → Confidence is capped at 0.30; extraction-failed error is returned.
- What happens when the same user submits the same place twice? → This is not in scope for Phase 2; no deduplication is required yet.
- What happens when `raw_input` is an empty string or only whitespace? → The endpoint returns a 400 error immediately, before any extraction attempt.
- What happens when a non-TikTok URL is submitted (e.g. an Instagram URL)? → The system returns an unsupported-input error. Instagram support is Phase 3.
- What happens when extraction succeeds but the places database write fails? → The endpoint returns a 500; no partial state is left behind.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST accept a raw input string (a TikTok URL or plain text) and a user identifier, and return a structured place record or a clear error.
- **FR-002**: The system MUST identify which extraction strategy to use based solely on the format of the input string — no manual classification by the caller is required.
- **FR-003**: For TikTok URLs, the system MUST retrieve the video caption from the public TikTok metadata endpoint and use it as the source for place extraction.
- **FR-004**: For plain text input (anything that is not an HTTP/HTTPS URL), the system MUST pass the text directly to the extraction model.
- **FR-005**: The system MUST use a language model to extract structured place fields — name, address, cuisine type, and price range — from the source text.
- **FR-006**: The system MUST validate the extracted place name against the places database and use the match result as an input to the confidence calculation.
- **FR-007**: The system MUST compute a confidence score deterministically from the extraction source type, the places database match quality, and whether multiple sources agree — the language model must not provide the confidence score.
- **FR-008**: If confidence is ≥ 0.70, the system MUST write the place record to the database and return it with `requires_confirmation: false`.
- **FR-009**: If confidence is between 0.30 (exclusive) and 0.70 (exclusive), the system MUST return the candidate place data with `requires_confirmation: true` without writing to the database.
- **FR-010**: If confidence is ≤ 0.30 (no places database match), the system MUST return an extraction-failed error response without writing any record.
- **FR-011**: All confidence thresholds and scoring weights MUST be configurable without code changes.
- **FR-012**: The system MUST return a well-structured error response for unsupported input types (non-TikTok URLs in Phase 2), distinguishing them from validation failures.
- **FR-013**: The system MUST support adding new input source types (e.g. Instagram, generic URLs) without modifying existing extraction logic or the endpoint handler.
- **FR-014**: The places database API key MUST be read from the environment at runtime and never stored in configuration files committed to version control.
- **FR-015**: All extracted and validated data MUST pass schema validation before any database write; no unvalidated data may be persisted.

### Key Entities

- **Place record**: A structured representation of a restaurant or venue. Internal primary key is a system-generated UUID, stable across validation provider changes. External provider identifiers (e.g. `google_place_id`) are stored as separate nullable columns — never used as PK. Key attributes: UUID, name, address, cuisine type, price range, `google_place_id` (nullable), source of extraction, confidence score, user who added it, and timestamp.
- **Extraction result**: The intermediate output of the language model extraction step. Contains candidate name, address, cuisine, and price range — not yet validated or persisted.
- **Places match result**: The outcome of validating an extraction result against the places database. Contains match quality level (exact, approximate, category-only, or none) and the canonical validated name if a match was found.
- **Extraction confidence**: A numeric score between 0.0 and 0.95 computed from the extraction source type, the places match quality, and multi-source agreement. Determines whether the place is saved, held for confirmation, or rejected.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For TikTok URLs with captions that mention a restaurant by name, the system identifies and saves the correct place with no user intervention in ≥ 85% of cases.
- **SC-002**: End-to-end response time for a successful extraction (TikTok URL → saved place record) is under 10 seconds under normal network conditions. The TikTok oEmbed call has a 3-second timeout; remaining budget covers LLM extraction and places validation.
- **SC-003**: End-to-end response time for plain text input is under 5 seconds under normal network conditions.
- **SC-004**: No invalid or unvalidated place record is ever written to the database — zero tolerance for data quality failures at the persistence boundary.
- **SC-005**: Adding a new input source type (Phase 3: Instagram, generic URLs) requires writing one new class and registering it in one location — no changes to existing extraction classes or the endpoint handler.
- **SC-006**: All confidence weights and thresholds are adjustable via configuration, with changes taking effect on the next server restart and no code deployment required.
- **SC-007**: The endpoint returns a machine-readable error type (not just an HTTP status code) for every failure mode, enabling the product system to show the correct user-facing message.

## Clarifications

### Session 2026-03-24

- Q: When `requires_confirmation: true` is returned, how does the user's confirmation produce a saved place record? → A: The product system calls this same endpoint again with the user-confirmed or corrected input. The endpoint is stateless; no separate confirmation flow or endpoint is needed.
- Q: What uniquely identifies a saved place record in the database? → A: System-generated UUID is always the internal PK. External provider IDs (e.g. `google_place_id`) are stored as nullable columns alongside it — never used as PK. Adding a new validation source (e.g. Foursquare) means adding a new nullable column; no migration of the PK is required.
- Q: What is the timeout threshold for the TikTok oEmbed call? → A: 3 seconds. Leaves sufficient budget for LLM extraction and Google Places validation within the 10s total ceiling (SC-002).

## Assumptions

- The places database (Google Places) provides sufficient coverage for the restaurants users are likely to share via TikTok in Phase 2.
- TikTok's public metadata endpoint remains available and returns usable captions for food review content. If it becomes unavailable, extraction fails gracefully — no fallback to video download or other techniques in Phase 2.
- Embedding generation is handled by a separate task (Voyage provider setup). This endpoint writes the place record but does not generate or store an embedding vector — that step is wired up elsewhere.
- The taste model update triggered by a new place save is handled asynchronously and is not part of this endpoint's response (per the decoupling decision already recorded in project decisions).
- Phase 2 supports TikTok URLs and plain text only. Instagram, generic URLs, voice input, and image OCR are Phase 3 and are explicitly out of scope.
- The `user_id` passed in the request is already validated by the product system (NestJS). This endpoint trusts it without re-authenticating.
