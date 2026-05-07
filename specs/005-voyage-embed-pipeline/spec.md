# Feature Specification: Embedding Pipeline — Embed Saved Places with Voyage

**Feature Branch**: `005-voyage-embed-pipeline`
**Created**: 2026-03-30
**Status**: Draft
**Input**: User description: "Embedding pipeline — embed saved places with Voyage. Build Alembic migration for VECTOR(1024), Embedder Protocol, VoyageEmbedder, EmbeddingRepository, and wire into extract-place service layer."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Place Saved With Embedding Immediately Available (Priority: P1)

When someone shares a place (via the product app), the AI engine saves the place and immediately generates a semantic embedding for it. By the time the save confirmation returns, the place's vector representation is already stored and available for similarity search.

**Why this priority**: Embedding must exist before any retrieval or recommendation can happen. Without this, the entire recall pipeline cannot function. Every saved place needs an embedding — there is no partial-value scenario.

**Independent Test**: Can be fully tested by submitting a place to the extract-place endpoint and confirming a 1024-dimensional vector row appears in the embeddings table before the response is received.

**Acceptance Scenarios**:

1. **Given** a valid place description is submitted, **When** the extract-place endpoint processes it successfully, **Then** a semantic vector for the place is stored and queryable before the response is returned to the caller.
2. **Given** the same place is submitted twice, **When** the extract-place endpoint processes the second submission, **Then** the dedup check finds the existing place and returns early — the existing embedding is preserved unchanged (no duplicate rows, no unnecessary re-embedding).
3. **Given** a place with no description text, **When** the extract-place endpoint processes it, **Then** the best available textual representation is embedded and stored.

---

### User Story 2 - Embedding Provider Swappable Without Code Changes (Priority: P2)

The embedding model can be changed in configuration without touching any business logic code. The system uses the configured provider transparently — nothing in the pipeline knows or cares which vendor generates the vectors.

**Why this priority**: Provider lock-in increases long-term cost and risk. Voyage 4-lite is the current choice based on retrieval quality benchmarks, but the ability to switch providers is a non-negotiable architectural constraint (ADR-038, ADR-020).

**Independent Test**: Can be fully tested by verifying no concrete embedding provider class is imported in any service or route file — only the abstract protocol is referenced from business logic.

**Acceptance Scenarios**:

1. **Given** the embedder provider is defined in configuration, **When** the pipeline generates an embedding, **Then** it uses whichever provider is configured without any code change required to switch.
2. **Given** a new embedding provider is introduced, **When** it is wired as a concrete implementation, **Then** no existing service, route, or orchestration code requires modification.

---

### User Story 3 - Embedding Calls Visible in Observability Dashboard (Priority: P3)

Every embedding call that the system makes is traceable. Operators can see embedding latency, input text, and which model was used — the same way LLM calls are already observable.

**Why this priority**: Silent embedding failures would corrupt retrieval quality invisibly. Observability is a production safety requirement, not a nice-to-have (ADR-025, ADR-043).

**Independent Test**: Can be fully tested by submitting a place and verifying a corresponding embedding trace appears in the Langfuse dashboard showing model, input, and duration.

**Acceptance Scenarios**:

1. **Given** an embedding call is made, **When** it completes, **Then** a trace record is written to the observability system with the model name, input type, and duration.
2. **Given** an embedding call fails, **When** the error is caught, **Then** the failure is recorded in the observability system — not silently dropped.

---

### Edge Cases

- What happens when the embedding service is unavailable? The extract-place response should fail with an appropriate error — a place saved without an embedding would be unretriavable, which is worse than a failed save.
- What happens when the place has no meaningful text to embed? The system embeds the best available combination of name, category, and address rather than failing.
- What happens when the same place is re-submitted? The dedup check finds the existing record and returns early — no new place row is written, no embedding is generated. The existing embedding is preserved.
- What happens when the vector dimension in the database does not match the model's output dimension? The migration must set dimensions to 1024 before any embedding is written — mismatch should be caught at startup.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST generate a semantic vector embedding for every successfully saved place before returning the extract-place response to the caller.
- **FR-002**: The system MUST store embeddings with exactly 1024 dimensions — the database schema MUST enforce this constraint.
- **FR-003**: The system MUST use the embedding model specified in configuration — model selection MUST NOT be hardcoded in any source file.
- **FR-004**: The system MUST attach an observability trace to every embedding call, capturing model name, input type, and duration.
- **FR-005**: The system MUST NOT create duplicate embedding rows — exactly one embedding row per place. Embedding is only generated when a new place row is written; re-submissions that hit the dedup path skip embedding entirely.
- **FR-006**: All embedding orchestration MUST be handled in the service layer — route handlers MUST NOT directly call the embedder or embedding storage.
- **FR-007**: The embedder abstraction MUST be defined as a protocol — no concrete embedding class MUST be imported directly in business logic or service code.
- **FR-008**: Embedding storage MUST go through a dedicated repository class — no raw database queries for embeddings outside that repository.
- **FR-009**: The database schema MUST be migrated to VECTOR(1024) before any embeddings are written — if the column is at a different dimension, the migration must correct it.
- **FR-010**: The system MUST support `document` input type for place descriptions and `query` input type for future search use — these are different embedding modes and MUST be distinguishable at call time.

### Key Entities

- **Embedding**: A 1024-dimensional vector representation of a place's textual content. Linked to a specific place by ID. Stores which model version produced it. Used for similarity search in the recall and consult pipelines.
- **Embedder (protocol)**: An abstract contract for any provider that can convert a list of text strings into a list of vectors. Accepts texts and an input type hint (`document` vs `query`). Any concrete provider implements this protocol.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Every place saved via the extract-place endpoint has a corresponding 1024-dimensional vector row in the embeddings table by the time the response is returned — 100% coverage, zero gaps.
- **SC-002**: Re-submitting the same place results in exactly one embedding row (upsert, not insert) — confirmed by row count remaining 1 after N submissions of the same place.
- **SC-003**: Swapping the embedding provider requires zero changes to service, route, or orchestration code — verified by the absence of concrete provider imports outside the provider layer.
- **SC-004**: Every embedding call produces a trace in the observability system — 100% trace coverage, verified by submitting a test place and confirming the trace appears.
- **SC-005**: All existing tests continue to pass after this feature is implemented — zero regressions in the test suite.
- **SC-006**: Static type checking passes with no errors across the entire source tree after this feature is implemented.

## Clarifications

### Session 2026-03-30

- Q: When the same place is re-submitted and the dedup check finds it already exists, should the embedding be regenerated? → A: No — dedup path returns early, existing embedding is preserved unchanged.
- Q: Should backfilling embeddings for pre-existing places (saved before this feature was deployed) be in scope? → A: Out of scope — document as a known gap; add a backfill script in a separate task when needed.

## Assumptions

- The embeddings table already exists in the database (created by prior migrations). Only the vector column dimension needs correction if it is not already 1024.
- The `voyage-4-lite` model produces 1024-dimensional vectors by default — no dimension truncation or padding is required.
- The Langfuse tracing infrastructure (`get_langfuse_client()` in `src/kebi/providers/tracing.py`) already exists and will be used directly via the low-level `generation()` API (voyageai has no LangChain callback support).
- The voyageai Python SDK (`voyageai ^0.3`) is already in `pyproject.toml` — no new dependency needed.
- The place description field (or equivalent text) is the primary input for generating embeddings. If description is absent, a combination of name, category, and address is used.
- No production embedding data exists yet, so the dimension migration is safe to run destructively if needed.
- **Known gap**: Places saved before this feature is deployed will have no embedding and will be invisible to the recall pipeline. Backfilling is explicitly out of scope here — a separate backfill script should be added as a follow-up task when needed.
