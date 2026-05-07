# Feature Specification: Streaming Recommendations via SSE

**Feature Branch**: `001-consult-streaming`
**Created**: 2026-03-17
**Status**: Draft
**Input**: User description: Add SSE streaming mode to POST /v1/consult. When stream=true, call the AI provider with a hardcoded system prompt and stream tokens back as SSE events token by token. When stream=false or absent, return the existing synchronous JSON stub.

## User Scenarios & Testing *(mandatory)*

<!--
  IMPORTANT: User stories should be PRIORITIZED as user journeys ordered by importance.
  Each user story/journey must be INDEPENDENTLY TESTABLE - meaning if you implement just ONE of them,
  you should still have a viable MVP (Minimum Viable Product) that delivers value.
  
  Assign priorities (P1, P2, P3, etc.) to each story, where P1 is the most critical.
  Think of each story as a standalone slice of functionality that can be:
  - Developed independently
  - Tested independently
  - Deployed independently
  - Demonstrated to users independently
-->

### User Story 1 - Real-Time AI Response Streaming (Priority: P1)

The product needs the AI's response to appear word by word as it is generated, rather than waiting for the complete answer. This makes the experience feel fast and interactive, especially for longer recommendation explanations.

**Why this priority**: Core to Phase 1 learning implementation. Demonstrates end-to-end streaming capability before the full recommendation pipeline is built. Improves perceived performance significantly — users see the first token almost immediately instead of waiting for a full round-trip.

**Independent Test**: Can be fully tested by sending a request with `"stream": true` and observing tokens appearing incrementally in real time, with a clear done signal at the end.

**Acceptance Scenarios**:

1. **Given** a request to `/v1/consult` with `"stream": true`, **When** the endpoint is called, **Then** the response begins emitting tokens immediately without waiting for the full AI response
2. **Given** streaming is active, **When** the AI generates text, **Then** each token arrives as a separate SSE event containing just that token
3. **Given** the AI finishes generating, **When** the last token is sent, **Then** a final `done` event is emitted to signal stream completion

---

### User Story 2 - Backward Compatible Synchronous Mode (Priority: P2)

Existing callers must continue to work unchanged. The endpoint must default to synchronous JSON response when streaming is not requested, ensuring zero disruption to current integrations.

**Why this priority**: Ensures backward compatibility during the transition to streaming. Allows phased rollout without requiring coordinated API version bumps.

**Independent Test**: Can be fully tested by sending a request to `/v1/consult` without the `"stream"` field and receiving a standard JSON response immediately.

**Acceptance Scenarios**:

1. **Given** a request without `"stream"` field, **When** the endpoint is called, **Then** a JSON response is returned (not streaming)
2. **Given** `"stream": false`, **When** the endpoint is called, **Then** a JSON response is returned (not streaming)
3. **Given** synchronous mode, **When** the response is received, **Then** it contains the complete recommendation in one response

---

### User Story 3 - Resource Cleanup on Client Disconnect (Priority: P1)

When a client disconnects mid-stream (e.g., user navigates away, network fails, timeout), the server must clean up streaming resources without leaking memory or leaving dangling async operations.

**Why this priority**: Critical for production reliability. Prevents memory leaks and resource exhaustion from abandoned streams. Ensures long-lived streaming connections don't degrade server performance.

**Independent Test**: Can be fully tested by initiating a streaming request and disconnecting before the final event, then verifying that all associated async resources are released.

**Acceptance Scenarios**:

1. **Given** a streaming request in progress, **When** the client connection is terminated, **Then** the async generator is properly cleaned up
2. **Given** cleanup on disconnect, **When** multiple clients disconnect simultaneously, **Then** no resource leaks or lingering connections remain
3. **Given** the endpoint is heavily used with streaming, **When** memory usage is monitored, **Then** it remains stable over time (no growth trend)

### Edge Cases

- What happens when a client connects but never reads events from the stream?
- How does the system handle a client that connects, reads a few tokens, then disconnects?
- What is the behavior if the AI provider fails mid-stream (e.g., rate limit, network error)?
- Can the client properly parse and handle partial SSE event sequences?
- What happens if the AI provider takes more than 30 seconds to begin responding?

## Requirements *(mandatory)*

<!--
  ACTION REQUIRED: The content in this section represents placeholders.
  Fill them out with the right functional requirements.
-->

### Functional Requirements

- **FR-001**: System MUST support an optional `"stream": true` parameter in `/v1/consult` requests
- **FR-002**: When `stream` is `true`, system MUST return an SSE (Server-Sent Events) response instead of JSON
- **FR-003**: When `stream` is `false` or absent, system MUST return a synchronous JSON response (existing behavior)
- **FR-004**: SSE response MUST use `text/event-stream` content type with proper headers (`Cache-Control: no-cache`, `X-Accel-Buffering: no`)
- **FR-005**: When streaming, system MUST call the configured AI provider using a hardcoded system prompt: "You are Kebi, an AI place recommendation assistant. Answer the user's query helpfully and concisely."
- **FR-006**: System MUST emit one SSE event per token: `data: {"token": "..."}`
- **FR-007**: System MUST emit a final done event after all tokens: `data: {"done": true}`
- **FR-008**: System MUST use the AI provider abstraction layer — model role resolved from config, no model names hardcoded
- **FR-009**: System MUST detect client disconnection during streaming and immediately terminate the AI call
- **FR-010**: Upon client disconnect, system MUST clean up all async resources (AI stream, generator) without leaking memory
- **FR-011**: Route handler MUST remain a facade with exactly one service call (no business logic in route file)
- **FR-012**: Service logic MUST reside in `src/kebi/core/consult/service.py`
- **FR-013**: System MUST NOT use SSE decorator libraries — raw `StreamingResponse` with manual headers only

### Key Entities

- **Streaming Request**: Extended `/v1/consult` request with optional `stream: boolean` field
- **Token Event**: SSE event containing a single AI-generated token: `{"token": "..."}`
- **Done Event**: Final SSE event signalling stream completion: `{"done": true}`
- **AI System Prompt**: Hardcoded instruction that gives the AI its Phase 1 persona and task context

## Success Criteria *(mandatory)*

<!--
  ACTION REQUIRED: Define measurable success criteria.
  These must be technology-agnostic and measurable.
-->

### Measurable Outcomes

- **SC-001**: `/v1/consult` with `"stream": true` returns the first token within 1 second of the request
- **SC-002**: Tokens stream continuously with no visible pauses between them once the AI starts responding
- **SC-003**: Client disconnect during streaming releases all async resources within 50ms
- **SC-004**: Synchronous mode (stream=false) continues to return JSON response in under 3 seconds (existing SLA maintained)
- **SC-005**: Over 50 concurrent streaming connections, memory usage remains stable with no growth trend
- **SC-006**: Integration tests in `tests/core/consult/` all pass, including streaming, disconnect, and AI error scenarios
- **SC-007**: Code passes static analysis: `ruff check`, `mypy --strict`
- **SC-008**: Bruno request file provides working example of streaming against `localhost:8000`

## Assumptions

- Phase 1 uses a hardcoded system prompt; LangGraph orchestration and real intent parsing connect in Phase 4
- The AI provider role `orchestrator` is already configured in `config/models.yaml`
- NestJS product repo will be updated to forward and handle SSE streaming responses (out of scope here)
- Client timeout and disconnect scenarios are handled by the async runtime and request lifecycle
- SSE is supported by all clients and proxies in the deployment path
