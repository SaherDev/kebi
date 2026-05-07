# Feature Specification: Unified Chat Router

**Feature Branch**: `017-unified-chat-router`  
**Created**: 2026-04-09  
**Status**: Draft  
**Input**: User description: "Add unified POST /v1/chat endpoint with intent router that classifies and dispatches to extract-place, consult, recall, or assistant pipelines, replacing all existing endpoints"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Send a message and get the right response (Priority: P1)

A user sends a natural-language message through any client. The system classifies the intent and routes to the correct pipeline — returning a place recommendation, saving a place, retrieving saved places, or answering a general question — without the caller needing to choose an endpoint.

**Why this priority**: This is the core value of the feature. All other stories depend on routing working correctly.

**Independent Test**: Can be tested end-to-end by sending each of the four canonical messages to POST /v1/chat and verifying the response type matches the expected intent.

**Acceptance Scenarios**:

1. **Given** message "cheap dinner nearby", **When** POST /v1/chat is called, **Then** response type is "consult" and message contains a recommendation
2. **Given** a TikTok URL in the message, **When** POST /v1/chat is called, **Then** response type is "extract-place" and message confirms the place was saved
3. **Given** "that ramen place I saved from TikTok", **When** POST /v1/chat is called, **Then** response type is "recall" and message returns matching saved places
4. **Given** "is tipping expected in Japan?", **When** POST /v1/chat is called, **Then** response type is "assistant" and message contains a helpful answer
5. **Given** any valid intent, **When** the downstream pipeline raises an exception, **Then** response type is "error" with a user-friendly message

---

### User Story 2 - Receive a clarification prompt for ambiguous input (Priority: P2)

A user sends an ambiguous message like "fuji" where the system cannot determine intent with sufficient confidence. Rather than guessing, the system returns a single clarifying question for the user to answer before proceeding.

**Why this priority**: Without clarification handling, ambiguous messages silently route to the wrong pipeline and produce incorrect results. This story protects response quality.

**Independent Test**: Can be tested by sending "fuji" to POST /v1/chat and verifying the response type is "clarification" with a non-null, single question.

**Acceptance Scenarios**:

1. **Given** a message with intent confidence below 0.7, **When** POST /v1/chat is called, **Then** response type is "clarification" and message contains a short, specific question
2. **Given** "fuji" as the message, **When** POST /v1/chat is called, **Then** the clarification question asks whether the user wants to recall a saved place or get a recommendation
3. **Given** an ambiguous message, **When** a clarification is returned, **Then** the clarification_question is exactly one question — never compound

---

### User Story 3 - Old endpoints are removed (Priority: P3)

Callers that previously used /v1/extract-place, /v1/consult, /v1/recall, or /v1/chat-assistant receive a 404. All functionality is available exclusively through /v1/chat.

**Why this priority**: Necessary for API hygiene after the migration is complete. Depends on P1 working.

**Independent Test**: Can be tested by calling any old endpoint and verifying 404 is returned.

**Acceptance Scenarios**:

1. **Given** a POST request to /v1/extract-place, **When** the request is sent, **Then** the server responds with 404
2. **Given** a POST request to /v1/consult, **When** the request is sent, **Then** the server responds with 404
3. **Given** a POST request to /v1/recall, **When** the request is sent, **Then** the server responds with 404
4. **Given** a POST request to /v1/chat-assistant, **When** the request is sent, **Then** the server responds with 404

---

### User Story 4 - Recommendation history is persisted (Priority: P4)

Every completed consult response is stored in a recommendations table alongside the query, intent, and user_id for future feedback and taste model improvement.

**Why this priority**: Enables feedback loops and taste model improvement. Does not affect the live response the user receives.

**Independent Test**: Can be tested by sending a "consult" message and querying the recommendations table to confirm a row was inserted with the correct user_id, query, intent, and response.

**Acceptance Scenarios**:

1. **Given** a completed consult response, **When** the response is returned to the caller, **Then** a record exists in the recommendations table with matching user_id, query, intent, and response
2. **Given** a clarification or error response, **When** POST /v1/chat is called, **Then** no record is written to recommendations

---

### Edge Cases

- What happens when the intent router LLM call times out or returns malformed JSON? → Response type is "error" with a user-friendly message; raw exception detail is included in data.
- What happens if location is omitted for a "consult" intent? → ConsultService receives null location and falls back to location-agnostic ranking.
- What happens if a message is empty or whitespace-only? → Intent router classifies the empty string; if confidence is below 0.7, a clarification is returned.
- What happens if the downstream service is unavailable? → Exception is caught by ChatService and returned as type "error".

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST expose a single POST /v1/chat endpoint accepting user_id, message, and optional location
- **FR-002**: System MUST classify each message into one of four intents: "extract-place", "consult", "recall", or "assistant"
- **FR-003**: System MUST return type "clarification" when intent confidence is below 0.7, with a single clarifying question
- **FR-004**: System MUST dispatch to the correct downstream pipeline based on classified intent
- **FR-005**: System MUST return a structured response with type, human-readable message, and optional data payload
- **FR-006**: System MUST catch all downstream exceptions and return type "error" with a user-friendly message
- **FR-007**: System MUST attach Langfuse tracing to the intent classification LLM call (ADR-025)
- **FR-008**: System MUST resolve the intent classifier model via the "intent_router" logical role in config — never hardcode a model name
- **FR-009**: All four old endpoints MUST be removed; their routes MUST return 404 after the migration
- **FR-010**: `ConsultService` MUST attempt to persist a `consult_logs` record before returning its result; if the write fails, the failure MUST be logged and the result MUST still be returned to the caller. Logging is ConsultService's responsibility, not ChatService's.
- **FR-011**: Clarification question MUST be a single, short, conversational question
- **FR-012**: System MUST pass location to the consult pipeline only; ignore it for all other intents
- **FR-013**: ConsultService MUST be callable via a non-streaming method that returns a complete response object; if no such method exists, it MUST be added as part of this feature

### Key Entities

- **ChatRequest**: Inbound conversational message — user_id, message text, optional location (lat/lng dict)
- **ChatResponse**: System reply — type (one of: extract-place, consult, recall, assistant, clarification, error), human-readable message, optional structured data dict
- **IntentClassification**: Internal router output — intent string, confidence score (0.0–1.0), clarification_needed flag, optional clarification question
- **Recommendation**: Persisted consult record — id (UUID), user_id, query, response payload (JSONB), intent, accepted (nullable bool), selected_place_id (nullable), created_at

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: All five message types (extract-place, consult, recall, assistant, clarification) return the correct response type on the canonical test inputs with 100% accuracy
- **SC-002**: Ambiguous messages (confidence < 0.7) always return type "clarification" — zero ambiguous messages silently misrouted
- **SC-003**: All four old endpoints return 404 after migration — zero legacy routes remain
- **SC-004**: Every completed consult response attempts to produce exactly one record in the recommendations table — no duplicates; write failures are logged but do not fail the caller response
- **SC-005**: All tests pass, ruff reports zero lint errors, and mypy reports zero type errors

## Clarifications

### Session 2026-04-09

- Q: When ChatService dispatches to ConsultService for a "consult" intent, does ConsultService return a complete response object (non-streaming) or does ChatService forward a streaming response? → A: ConsultService exposes (or gains) a non-streaming method; ChatService awaits a complete response object (Option B).
- Q: If the Recommendation DB write fails after a successful consult, should the error propagate to the caller or be swallowed? → A: Return the consult result to the caller anyway; log the write failure but do not surface it as an error (Option A).
- Q: Should /v1/chat validate that user_id refers to a real user? → A: No — user_id is a Clerk-issued value; the product repo owns auth and validates the caller before forwarding. This repo trusts user_id as-is.

## Assumptions

- user_id is a Clerk-issued identifier; the product repo (NestJS + Clerk) authenticates the caller before forwarding to this repo. This repo never validates user_id — it is passed through to downstream services as-is
- ExtractionService, ConsultService, RecallService, and AssistantService exist with stable interfaces
- "intent_router" logical role is not yet in config/app.yaml; it must be added pointing to llama-3.1-8b-instant via the Groq provider for ~100ms classification latency
- The recommendations table does not yet exist; an Alembic migration is required
- The previous /v1/consult used streaming (SSE); the unified /v1/chat endpoint is non-streaming JSON. If ConsultService currently only exposes a streaming interface, a non-streaming adapter method must be added as part of this feature
- Bruno collection exists at kebi-config/bruno/; .bru files for old endpoints will be deleted and a new chat.bru added
- The location field is a plain dict (not a typed sub-model) to match the existing consult contract
