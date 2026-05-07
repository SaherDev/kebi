# Research: LLM NER Enricher Redesign

**Feature**: 014-ner-enricher-redesign  
**Date**: 2026-04-08

## Finding 1: `_city_filter.py` Is Deleted Entirely

**Decision**: Delete `_city_filter.py`. Also update `emoji_regex.py` to remove its `CITY_BLOCKLIST` import and the filtering logic in `_extract_city_hint()`.  
**Rationale**: The blocklist was code-level compensation for what the LLM should handle natively. Now that all metadata (hashtags, platform, location_tag) is passed structurally to the LLM, city correctness is the model's responsibility. The blocklist is dead weight — it doesn't belong in the enricher layer at all.  
**Impact**: `emoji_regex.py` must be updated: remove `from kebi.core.extraction.enrichers._city_filter import CITY_BLOCKLIST` and update `_extract_city_hint()` to omit the blocklist filter (or simplify/remove the method entirely if the blocklist was its only guard).  
**Alternatives considered**: Keeping `_city_filter.py` for `emoji_regex.py` — rejected by user; the blocklist is unnecessary code that should be deleted.

## Finding 2: Langfuse API — `get_langfuse_client()` (not `get_langfuse_handler()`)

**Decision**: Keep using `get_langfuse_client()` from `providers/tracing.py`.  
**Rationale**: `providers/tracing.py` only exports `get_langfuse_client()`. There is no `get_langfuse_handler()` function. The task spec referenced the wrong name. The existing Langfuse integration pattern (call `langfuse.generation(...)` then `.end()`) is correct and should be preserved.  
**Alternatives considered**: Adding `get_langfuse_handler()` — rejected; unnecessary file churn. The current API is sufficient and works.

## Finding 3: `ExtractionContext` Missing 4 Fields

**Decision**: Add `platform: str | None = None`, `title: str | None = None`, `hashtags: list[str] = field(default_factory=list)`, `location_tag: str | None = None` to the `ExtractionContext` dataclass in `src/kebi/core/extraction/types.py`.  
**Rationale**: The enricher needs these fields from context. They do not exist today. Adding optional fields with safe defaults is backwards-compatible — no existing caller sets them, so all existing `ExtractionContext(...)` constructors continue to work.  
**Impact**: All tests constructing `ExtractionContext` still pass (defaults cover missing args).

## Finding 4: `CandidatePlace` Missing 2 Fields

**Decision**: Add `price_range: str | None = None` and `place_type: str | None = None` to the `CandidatePlace` dataclass in `src/kebi/core/extraction/types.py`.  
**Rationale**: The new `NERPlace` schema includes these fields and they should be carried forward to `CandidatePlace` without loss. Optional with `None` defaults is backwards-compatible.  
**Impact**: All existing `CandidatePlace(name=..., city=..., cuisine=..., source=...)` constructors continue to work; `price_range` and `place_type` default to `None`.

## Finding 5: XML Tag Change — `<context>` → `<metadata>`

**Decision**: User message wraps all fields in `<metadata>...</metadata>` (not `<context>`).  
**Rationale**: The task spec explicitly requires `<metadata>` block format with structured key-value fields (platform, title, caption, hashtags, location_tag). This replaces the previous single `<context>` raw-text wrap. ADR-044 compliance is maintained — system prompt still contains the defensive instruction.  
**Impact**: The existing test `test_adr_044_context_xml_tags_in_user_message` checks for `<context>` tags; this test must be updated to check for `<metadata>` tags instead.

## Finding 6: Test File Needs Major Surgery

**Decision**: Remove `TestSanitizeCity` and `TestCityExtractionScenarios` classes entirely from `test_llm_ner.py`. These test the old `_sanitize_city` post-processing behavior being deleted. Since `_city_filter.py` is also deleted, no new test file for it is needed.  
**Rationale**: Once `_sanitize_city` is removed from `llm_ner.py` and `_city_filter.py` is deleted, those tests are testing dead code.  
**New tests to add**: Case 1 (no text → skip), Case 2 (supplementary_text only → runs), Case 3 (caption + full metadata → full metadata block passed), structured fields (price_range, place_type) on candidates, `<metadata>` tag in user message.

## Finding 7: No Changes to Provider Layer Needed

**Decision**: The Instructor client's `extract()` call signature is unchanged. Only `messages` and `response_model` change.  
**Rationale**: `InstructorClient.extract(response_model=..., messages=[...])` is the same call pattern. No new provider config, no new logical role — `intent_parser` role (GPT-4o-mini) continues to be used.
