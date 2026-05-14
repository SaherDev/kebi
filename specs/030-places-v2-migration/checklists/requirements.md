# Specification Quality Checklist: Extraction → Places v2 Cutover

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-05-13
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Notes

- Scope narrowed to the extraction flow only per follow-up direction. Read-side migration (recall, consult, agent recall, user-places API, taste model) is intentionally deferred.
- Four clarifications resolved on 2026-05-13 (see spec's `## Clarifications` section): (1) in-flight extractions at cutover drain on the previous version (rolling-deploy semantics); (2) `core/places_v2/` is frozen for this feature — all changes happen on the extraction side, captured as FR-010a and a new Out-of-Scope bullet; (3) extraction search delegates to `places_v2.PlacesSearchService` per ADR-070 (`searcher.py` deleted); (4) `ExtractionPersistenceService` dropped per ADR-071 (supersedes ADR-057) — `upsert_and_embed` + `save_places` inlined, confidence partition deprecated, all picker outputs saved with `approved=False`. LLMPlacePicker output schema explicitly rewritten to v2 vocabulary (`PlaceCategory`, `PlaceTag`, namespaced `provider_id`).
- One key scope decision resolved up-front: extraction performs a clean cut to v2 (no dual-write, no temporary read shim). Captured as an Assumption rather than a [NEEDS CLARIFICATION] marker.
- Behavioral parity (confidence-band partitioning, duplicate merge policy, embedding diff-then-embed) is asserted as a hard requirement (FR-004 through FR-006) and as a measurable success criterion (SC-007).
- External contract preservation is asserted both as a hard requirement (FR-012) and as a measurable success criterion (SC-006).
- The accepted temporary user-visible regression (save → recall loop broken until follow-up feature) is explicit in Assumptions and Edge Cases so it cannot be misread as a defect.
- Spec uses neutral phrasing ("v2 store", "v2 cache shape", "extraction flow") rather than naming concrete files, classes, or table names. Concrete bindings belong in the implementation plan.
