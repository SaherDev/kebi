# Feature Specification: Extraction Cascade Foundation — Phases 1–4

**Feature Branch**: `010-extraction-cascade-run1`
**Created**: 2026-04-06
**Status**: Draft
**Input**: Phases 1–4 of extraction cascade migration — pure additive foundation run

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Emoji-tagged place extracted from TikTok caption (Priority: P1)

A developer can pass an `ExtractionContext` with a TikTok caption containing `📍PlaceName` markers to `EmojiRegexEnricher`. The enricher populates `context.candidates` with one `CandidatePlace` per marker — no LLM, no HTTP, instant. A caption with three `📍` tags produces three candidates.

**Why this priority**: Emoji-tagged locations are the highest-confidence extraction signal. This path is pure computation — if it fails, nothing downstream can compensate.

**Independent Test**: Instantiate `EmojiRegexEnricher`, create `ExtractionContext` with a caption containing known markers, call `enrich()`, assert candidates list. No external services needed.

**Acceptance Scenarios**:

1. **Given** `context.caption = "Best ramen 📍Fuji Ramen 🍜 #bangkok"`, **When** `EmojiRegexEnricher.enrich(context)` is called, **Then** `context.candidates` has one entry with `name="Fuji Ramen"` and `source=ExtractionLevel.EMOJI_REGEX`
2. **Given** a caption with three `📍` markers, **When** `enrich()` is called, **Then** `context.candidates` has exactly three entries
3. **Given** `context.caption = None` and `context.supplementary_text = ""`, **When** `enrich()` is called, **Then** `context.candidates` remains empty and no error is raised
4. **Given** `context.candidates` already contains one candidate, **When** `enrich()` is called on a caption with a `📍` marker, **Then** the existing candidate is preserved and the new one is appended

---

### User Story 2 — LLM extracts all place names from caption without skipping (Priority: P1)

A developer can pass a context with caption text to `LLMNEREnricher`. It calls GPT-4o-mini, receives a structured list of place names, and appends them as candidates. It runs even when `context.candidates` is already populated, ensuring places mentioned in text (not tagged) are captured alongside tagged ones.

**Why this priority**: LLM NER is the primary source for untagged places and the enabler of multi-place extraction. The no-skip-guard rule is critical — skipping it would miss places that appear in text but not in `📍` markers.

**Independent Test**: Mock the LLM call. Pass a context with existing candidates. Assert new candidates are appended and the LLM was called regardless.

**Acceptance Scenarios**:

1. **Given** `context.caption` contains two place names and `context.candidates` already has one entry from regex, **When** `LLMNEREnricher.enrich(context)` is called, **Then** both places from the LLM are appended — total candidates is now 3
2. **Given** the LLM call is observed, **Then** the system prompt contains a defensive instruction against following instructions in user content, and the caption content is wrapped in `<context>...</context>` XML tags in the user message
3. **Given** `context.caption = None` and `context.supplementary_text = ""`, **When** `enrich()` is called, **Then** no LLM call is made and `context.candidates` is unchanged
4. **Given** the LLM returns an empty places array, **When** `enrich()` processes the response, **Then** no candidates are appended and no error is raised

---

### User Story 3 — TikTok oEmbed fetch protected by circuit breaker (Priority: P2)

A developer wraps `TikTokOEmbedEnricher` in `CircuitBreakerEnricher`. When TikTok's endpoint fails five consecutive times, the circuit opens. Subsequent calls return immediately without hitting the endpoint. After a cooldown, one probe request is allowed. A successful probe closes the circuit.

**Why this priority**: TikTok oEmbed breaks regularly during TikTok platform changes. Without circuit breaking, every extraction request stalls waiting for a doomed timeout during outages.

**Independent Test**: Mock the HTTP client. Trigger `failure_threshold` exceptions. Assert the (threshold + 1)th call does not invoke the mock at all.

**Acceptance Scenarios**:

1. **Given** `failure_threshold=5` and the enricher raises on 5 consecutive calls, **When** the 6th call arrives, **Then** `enrich()` returns immediately without calling the wrapped enricher
2. **Given** the circuit is open and the cooldown has elapsed, **When** the next call arrives, **Then** the circuit enters half-open state and allows one probe through
3. **Given** a successful probe in half-open state, **When** the enricher returns normally, **Then** the circuit closes and failure count resets to zero
4. **Given** the enricher returns normally (None) without raising, **When** this occurs repeatedly, **Then** the failure count is never incremented regardless of how many times it returns None
5. **Given** `context.caption` is already set and oEmbed returns a caption, **When** `TikTokOEmbedEnricher.enrich()` is called, **Then** `context.caption` is NOT overwritten

---

### User Story 4 — Confidence scored per enricher level with multiplicative formula (Priority: P2)

A developer can call `calculate_confidence()` with an `ExtractionLevel`, a float match modifier, a corroboration flag, and a `ConfidenceConfig` loaded from `app.yaml`. The function returns the exact score from `(base * match_modifier) + corroboration_bonus`, capped at 1.0. All base scores come from config — no floats hardcoded in the function.

**Why this priority**: Every downstream threshold decision (save silently vs. require confirmation vs. reject) depends on this score being correct. An additive vs. multiplicative error here silently breaks the entire pipeline.

**Independent Test**: Pure function — no external dependencies. Pass known inputs, assert exact float output.

**Acceptance Scenarios**:

1. **Given** `source=EMOJI_REGEX`, `match_modifier=1.0`, `corroborated=False`, base score `0.95`, **When** `calculate_confidence()` is called, **Then** result is `0.95`
2. **Given** `source=EMOJI_REGEX`, `match_modifier=1.0`, `corroborated=True`, bonus `0.10`, **When** called, **Then** result is `min(0.95 + 0.10, 1.0) = 1.0`
3. **Given** `source=VISION_FRAMES`, `match_modifier=0.3`, `corroborated=False`, base score `0.55`, **When** called, **Then** result is `0.55 * 0.3 = 0.165`
4. **Given** `source=LLM_NER`, `match_modifier=0.6`, `corroborated=False`, base score `0.80`, **When** called, **Then** result is `0.80 * 0.6 = 0.48`

---

### Edge Cases

- What happens when `EmojiRegexEnricher` finds a `#hashtag` that looks like a city but has no adjacent `📍` marker? The hashtag city hint is extracted; if no place name is identifiable, no candidate is added.
- What happens when `LLMNEREnricher` receives a malformed LLM response? Pydantic validation via Instructor rejects the response; no candidates are appended; the error may be logged but does not propagate.
- What happens when `TikTokOEmbedEnricher` is called with `context.url = None`? Returns immediately with no HTTP call made.
- What happens when `CircuitBreakerEnricher` wraps an enricher that returns normally (None)? Failure count is NOT incremented. Circuit stays closed.
- What happens when `ParallelEnricherGroup` runs and one enricher raises? The exception propagates from `asyncio.gather`; the circuit breaker wrapping that enricher is responsible for catching it. `ParallelEnricherGroup` itself does not suppress exceptions.
- What happens when `calculate_confidence()` receives an `ExtractionLevel` not in `config.base_scores`? Falls back to `0.50` default base score.
- What happens when `app.yaml` is missing the new `extraction.confidence` block? `ExtractionConfig` load fails with a validation error at startup — fast fail, not a silent default.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST define `ExtractionLevel`, `CandidatePlace`, `ExtractionContext`, `ExtractionResult`, `ProvisionalResponse`, and `ExtractionPending` as dataclasses in a new `types.py` with zero imports from existing extraction modules
- **FR-002**: The system MUST provide `calculate_confidence(source, match_modifier, corroborated, config) -> float` using `min((base * match_modifier) + corroboration_bonus, 1.0)`
- **FR-003**: `ConfidenceConfig.base_scores` MUST be populated from `app.yaml` with string keys mapped to `ExtractionLevel` enum values at config-load time — no float literals in the function body
- **FR-004**: `ExtractionConfig` MUST include `confidence: ConfidenceConfig`, `circuit_breaker_threshold: int`, and `circuit_breaker_cooldown: float` alongside the existing `confidence_weights` and `thresholds` — no existing field removed
- **FR-005**: The system MUST add an `Enricher` Protocol with `async enrich(context: ExtractionContext) -> None` to `protocols.py` without removing `InputExtractor`
- **FR-006**: `EmojiRegexEnricher` MUST find ALL `📍PlaceName`, `@PlaceName`, and location hashtag matches in `context.caption or context.supplementary_text` and append one `CandidatePlace(source=ExtractionLevel.EMOJI_REGEX)` per match
- **FR-007**: `LLMNEREnricher` MUST run regardless of existing entries in `context.candidates` — no skip guard
- **FR-008**: `LLMNEREnricher` MUST include a defensive system prompt instruction and wrap caption in `<context>...</context>` XML tags in the user message (ADR-044)
- **FR-009**: `LLMNEREnricher` MUST attach a Langfuse callback handler on every LLM call (ADR-025)
- **FR-010**: `CircuitBreakerEnricher` MUST trip only on exceptions — a normal `None` return MUST NOT increment the failure counter
- **FR-011**: `CircuitBreakerEnricher` MUST implement half-open probe behavior after cooldown
- **FR-012**: `ParallelEnricherGroup` MUST run all enrichers via `asyncio.gather` and wait for all — no cancel-on-success
- **FR-013**: `TikTokOEmbedEnricher` MUST implement first-write-wins: skip if `context.caption` is already set
- **FR-014**: `TikTokOEmbedEnricher` and `YtDlpMetadataEnricher` MUST NOT catch exceptions internally
- **FR-015**: `YtDlpMetadataEnricher` MUST invoke `yt-dlp --dump-json {url}` as a subprocess and read the `description` field
- **FR-016**: All new classes with external dependencies MUST use constructor injection only
- **FR-017**: No existing production file (`service.py`, `places_client.py`, `result.py`, `dispatcher.py`, `extractors/tiktok.py`, `extractors/plain_text.py`) MUST be modified or deleted in this run
- **FR-018**: All new and modified files MUST pass `mypy --strict` and `ruff check`

### Key Entities

- **ExtractionLevel**: Enum of five enricher levels; keys into `ConfidenceConfig.base_scores`; carried on every `CandidatePlace`
- **CandidatePlace**: Unvalidated place name with enricher provenance and corroboration state; the unit enrichers produce and the validator consumes
- **ExtractionContext**: Shared mutable state threaded through all enrichers; accumulates `caption`, `transcript`, and `candidates`
- **ConfidenceConfig**: Typed config holding per-level base scores and corroboration bonus; loaded from `app.yaml`; injected into `calculate_confidence()`
- **CircuitBreakerEnricher**: Enricher wrapper tracking consecutive exception counts; skips wrapped enricher when circuit is open

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: All new unit tests pass — zero failures across `test_types.py`, `test_confidence_new.py`, `test_circuit_breaker.py`, and all enricher tests
- **SC-002**: Full existing test suite continues to pass — zero regressions
- **SC-003**: `mypy --strict` reports zero errors on all new and modified files
- **SC-004**: `ruff check src/ tests/` reports zero violations
- **SC-005**: `EmojiRegexEnricher` correctly identifies all three candidates in a caption with three `📍` markers in a single call — verified by test
- **SC-006**: `CircuitBreakerEnricher` opens after exactly `failure_threshold` consecutive exceptions and does NOT open after the same count of normal `None` returns — verified by two separate test cases
- **SC-007**: `calculate_confidence()` output matches the reference examples in `extraction-levels-cascade-reference.md` for all documented input combinations — verified by parameterized test

## Assumptions

- `get_langfuse_client()` (not `get_langfuse_handler()`) is the correct import from `kebi.providers.tracing`; `LLMNEREnricher` creates a manual generation span via `client.generation(...)` (resolved in research R-001)
- `get_llm("intent_parser")` and `get_instructor_client("intent_parser")` are available from `kebi.providers.llm` as used by existing extractors
- `yt-dlp` is available as a CLI command in the execution environment
- Adding new optional fields with defaults to `ExtractionConfig` is backward-compatible — existing `service.py` consumers continue to work unchanged
- `app.yaml` is updated with the new `extraction.confidence` and `circuit_breaker_*` fields as part of this run; if absent, startup fails fast rather than silently defaulting

## Clarifications

### Session 2026-04-06

- Q: FR-003 says `base_scores` keys should be "mapped to ExtractionLevel enum values at config-load time" — does the internal `ConfidenceConfig` type use `dict[ExtractionLevel, float]` (enum-keyed) or `dict[str, float]` (string-keyed)? → A: `dict[str, float]` with `.value` lookup in `calculate_confidence()`. Pydantic enum-keyed dicts require custom validators for YAML loading; string keys map directly to YAML without friction. (Resolved in research R-003.)
- Q: Should `calculate_confidence()` cap at `1.0` or a configurable `max_score`? → A: Configurable `max_score: float = 0.97` in `ConfidenceConfig`. No extraction path earns 1.0 — even perfect emoji regex + exact Google match involves two fallible steps.
- Q: Should `TikTokOEmbedEnricher` read its base URL and timeout from `config.external_services.tiktok_oembed`? → A: No. Hardcode `https://www.tiktok.com/oembed` and timeout `10.0s` as module-level constants. No config dependency means no missing-key risk at startup.
