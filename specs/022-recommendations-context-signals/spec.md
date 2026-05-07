# Feature Specification: Recommendations Persistence, User Context, and Signal Verification

**Feature Branch**: `022-recommendations-context-signals`
**Created**: 2026-04-17
**Status**: Draft
**Input**: User description: "Extend consult to persist recommendations, build GET /v1/user/context, verify POST /v1/signal"

## Clarifications

### Session 2026-04-17

- Q: Should the new `recommendations` table coexist with `consult_logs`, replace it, or be a rename? → A: Rename `consult_logs` to `recommendations` via migration. No `shown`/`accepted` columns — signal tracking lives in the taste/interaction tables.
- Q: Should `POST /v1/signal` replace the existing `POST /v1/feedback` or coexist? → A: Replace. Delete `/v1/feedback`, build `/v1/signal` as the sole signal endpoint.
- Q: Can a user send duplicate or conflicting signals for the same recommendation_id? → A: Accept all. Append-only, no uniqueness constraint on (recommendation_id, signal_type).
- Q: Should `place_id` in the signal request be validated against the places table? → A: No. Only validate `recommendation_id` exists. `place_id` is trusted, passed through to handler.
- Q: How is `recommendations.id` generated? → A: Database default UUID (existing `ConsultLog` pattern). Service reads back the generated ID after insert.
- Q: Is `place_id` required or optional on signal requests? → A: Required for both `recommendation_accepted` and `recommendation_rejected`.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Consult Persists Recommendation with Trackable ID (Priority: P1)

A user asks Kebi for a dining recommendation. The system classifies intent as consult, runs the pipeline, and returns results. Before returning, the system persists the recommendation to the `recommendations` table (renamed from `consult_logs`) and includes the database-generated `recommendation_id` in the response. The product repo uses this ID to track whether the user acts on the recommendation.

**Why this priority**: Without a persisted recommendation record, there is no way to link a signal (accepted/rejected) back to the specific consult response that produced it. This is the foundation Task 2 (context) reads from and Task 3 (signals) validates against.

**Independent Test**: Send a consult request via `POST /v1/chat` with a consult-intent message. Verify the response includes a top-level `recommendation_id` and a corresponding row exists in the `recommendations` table.

**Acceptance Scenarios**:

1. **Given** a user sends a consult message, **When** the consult pipeline completes, **Then** the response includes a top-level `recommendation_id` string and the `recommendations` table contains a row with that ID, the user's ID, the query text, the full response as JSON, and a timestamp.
2. **Given** a consult pipeline run, **When** the database write fails, **Then** the consult response is still returned to the caller (write failure does not block the response) and the failure is logged.
3. **Given** two consecutive consult requests from the same user, **When** both complete, **Then** each response has a unique `recommendation_id` (one row per consult call, not per result).

---

### User Story 2 - Product App Fetches User Context (Priority: P2)

The product app needs to display the user's taste profile chips and saved places count on a home screen or profile view. It calls `GET /v1/user/context?user_id=<id>` and receives a lightweight payload with saved count and precomputed taste chips.

**Why this priority**: The user context endpoint enables the product UI to show personalization signals without duplicating taste model logic in the product repo. It is independent of recommendation tracking but builds on the existing taste profile infrastructure.

**Independent Test**: Call `GET /v1/user/context?user_id=<id>` for a user with saved places and a taste profile. Verify the response includes `saved_places_count` matching `signal_counts.totals.saves` and the `chips` array matching the taste profile's precomputed chips.

**Acceptance Scenarios**:

1. **Given** a user with a taste profile containing 12 saves and 4 chips, **When** the product app calls `GET /v1/user/context?user_id=<id>`, **Then** the response contains `saved_places_count: 12` and a `chips` array with 4 entries, each having `label`, `source_field`, `source_value`, and `signal_count`.
2. **Given** a user with no taste profile (new user, no saves), **When** the product app calls `GET /v1/user/context?user_id=<id>`, **Then** the response contains `saved_places_count: 0` and an empty `chips` array.
3. **Given** a request with no `user_id` query parameter, **When** the endpoint is called, **Then** the system returns a 422 validation error.

---

### User Story 3 - Signal Endpoint Replaces Feedback Endpoint (Priority: P3)

When a user accepts or rejects a recommendation in the product UI, the product repo sends a signal to `POST /v1/signal` (replacing the former `POST /v1/feedback`). The signal handler verifies the `recommendation_id` exists in the `recommendations` table before processing. Both `place_id` and `recommendation_id` are required. Duplicate or conflicting signals for the same recommendation are accepted (append-only). `place_id` is trusted and not validated against the places table.

**Why this priority**: Depends on Task 1 (the recommendations table must exist with data). Verifying and completing the signal endpoint closes the feedback loop from recommendation to taste model update.

**Independent Test**: POST a signal with a valid `recommendation_id` and verify 202. POST a signal with a bogus `recommendation_id` and verify 404.

**Acceptance Scenarios**:

1. **Given** a valid `recommendation_id` from a prior consult, **When** the product repo sends `POST /v1/signal` with `signal_type: "recommendation_accepted"`, `user_id`, `recommendation_id`, and `place_id`, **Then** the system returns 202 Accepted and the taste model handler fires in the background.
2. **Given** a valid `recommendation_id`, **When** the product repo sends `POST /v1/signal` with `signal_type: "recommendation_rejected"`, `user_id`, `recommendation_id`, and `place_id`, **Then** the system returns 202 Accepted and the rejection handler fires in the background.
3. **Given** a `recommendation_id` that does not exist in the `recommendations` table, **When** a signal is sent, **Then** the system returns 404.
4. **Given** an unknown `signal_type` (not `recommendation_accepted` or `recommendation_rejected`), **When** a signal is sent, **Then** the system returns 422.
5. **Given** a valid signal is dispatched, **When** the background handler runs, **Then** a Langfuse trace is created for the handler execution.
6. **Given** a user sends `recommendation_accepted` and then `recommendation_rejected` for the same `recommendation_id`, **When** both signals are processed, **Then** both are accepted and appended to the interaction log (no uniqueness constraint).

---

### Edge Cases

- What happens when the recommendation write fails during a consult? The consult response is still returned. The failure is logged. No `recommendation_id` is included in the response.
- What happens when `GET /v1/user/context` is called for a user_id that exists in the system but has no taste profile? Return `saved_places_count: 0` and empty `chips` array.
- What happens when the EventDispatcher background handler fails after a signal is accepted? The 202 is already returned (fire-and-forget). The failure is logged via Langfuse and the app logger.
- What happens when `POST /v1/signal` is called with `signal_type: "onboarding_signal"`? Out of scope for this feature. Return 422.
- What happens when duplicate signals are sent for the same recommendation? All are accepted and appended (append-only, no deduplication).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST rename the `consult_logs` table to `recommendations` via an Alembic migration. The schema remains: `id` (UUID PK, database-generated default), `user_id` (string, indexed), `query` (text), `response_json` (JSONB), `created_at` (timestamptz, server default). No `shown` or `accepted` columns.
- **FR-002**: `ConsultService` MUST write one row to `recommendations` per consult call and include the database-generated `recommendation_id` in the response.
- **FR-003**: The recommendation write MUST happen inside the service layer, not in the route handler.
- **FR-004**: A recommendation write failure MUST NOT block the consult response. Failures are logged.
- **FR-005**: `ConsultResponse` MUST include a top-level `recommendation_id: str` field.
- **FR-006**: `GET /v1/user/context` MUST accept `user_id` as a query parameter and return `saved_places_count` and `chips` (no `user_id` in response body).
- **FR-007**: The user context handler MUST be a facade: one service call, no infrastructure in the route.
- **FR-008**: `saved_places_count` MUST be derived from the precomputed taste profile saved count (`signal_counts.totals.saves`), not a direct database count query.
- **FR-009**: `chips` MUST pass through as-is from the taste profile with no regeneration.
- **FR-010**: For a user with no taste profile, `GET /v1/user/context` MUST return `saved_places_count: 0` and empty `chips`.
- **FR-011**: `POST /v1/signal` MUST accept `signal_type` (string), `user_id` (string), `recommendation_id` (string), and `place_id` (string) in the request body. All fields are required.
- **FR-012**: `POST /v1/signal` MUST validate that `recommendation_id` exists in the `recommendations` table. Return 404 if not found. `place_id` is NOT validated against the places table.
- **FR-013**: `POST /v1/signal` MUST return 202 Accepted for valid signals (`recommendation_accepted`, `recommendation_rejected`).
- **FR-014**: `POST /v1/signal` MUST return 422 for unknown `signal_type` values.
- **FR-015**: Signal handlers MUST be dispatched via EventDispatcher and run as background tasks (fire-and-forget).
- **FR-016**: Signal handlers MUST produce Langfuse traces for observability.
- **FR-017**: The signal route handler MUST be a facade: validation and dispatch only, no business logic.
- **FR-018**: `POST /v1/feedback` route MUST be deleted. `POST /v1/signal` is the sole signal endpoint.
- **FR-019**: Duplicate signals for the same `recommendation_id` MUST be accepted (append-only, no uniqueness constraint).

### Key Entities

- **Recommendation**: Renamed from `consult_logs`. Persists a consult response with database-generated UUID. Links a `recommendation_id` to the user, query, and full response JSON. One row per consult call. No behavioral tracking columns — signal tracking lives in the interaction log and taste model.
- **UserContext**: Read-only aggregate of a user's taste profile summary data: saved places count and taste chips. Not persisted as its own entity; assembled from the existing taste model.
- **Signal**: A behavioral event (accepted/rejected) referencing a specific recommendation by ID. Both `recommendation_id` and `place_id` are required. Processed asynchronously after acknowledgment. Append-only — no deduplication.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Every consult response includes a `recommendation_id` and a corresponding database record exists within 1 second of the response being sent.
- **SC-002**: `GET /v1/user/context` returns valid JSON for any user within 500ms, including users with no taste profile (cold start returns zeros and empty arrays).
- **SC-003**: Signals referencing valid recommendations are acknowledged with 202 and produce a Langfuse trace within 5 seconds of dispatch.
- **SC-004**: Signals referencing non-existent recommendations are rejected with 404 -- zero orphan signals in the interaction log.
- **SC-005**: All three endpoints pass lint, type checking, and the database migration applies cleanly.
