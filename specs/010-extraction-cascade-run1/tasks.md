# Tasks: Extraction Cascade Foundation — Phases 1–4

**Input**: Design documents from `specs/010-extraction-cascade-run1/`
**Plan**: [plan.md](plan.md) | **Spec**: [spec.md](spec.md) | **Data Model**: [data-model.md](data-model.md)

> **For agentic workers:** Read `plan.md` in full before starting — it contains complete code for every file. This checklist is the execution tracker; the plan is the implementation reference.

## Format: `[ID] [P?] [Story?] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this implements

---

## Phase 1: Setup

**Purpose**: Confirm working environment and baseline.

- [x] T001 Confirm branch is `010-extraction-cascade-run1` (`git branch --show-current`)
- [x] T002 Run existing test suite to establish baseline: `poetry run pytest --tb=short -q`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Shared types layer and Enricher Protocol — required by every story phase.

**⚠️ CRITICAL**: All user story phases depend on T003–T011. Complete this phase before any story work.

- [x] T003 Create `src/kebi/core/extraction/types.py` per plan.md §Phase 1 Task 1.1 Step 1
- [x] T004 Create `tests/core/extraction/test_types.py` per plan.md §Phase 1 Task 1.1 Step 2
- [x] T005 [P] Create `src/kebi/core/extraction/enrichers/__init__.py` (empty file)
- [x] T006 [P] Create `tests/core/extraction/enrichers/__init__.py` (empty file)
- [x] T007 Add `Enricher` Protocol to `src/kebi/core/extraction/protocols.py` per plan.md §Phase 3 Task 3.1 Step 1
- [x] T008 Run `poetry run pytest tests/core/extraction/test_types.py -v`
- [x] T009 Run `poetry run mypy src/kebi/core/extraction/types.py src/kebi/core/extraction/protocols.py --strict`
- [x] T010 Run `poetry run ruff check src/kebi/core/extraction/types.py src/kebi/core/extraction/protocols.py`
- [x] T011 Commit: `git add src/kebi/core/extraction/types.py src/kebi/core/extraction/protocols.py src/kebi/core/extraction/enrichers/__init__.py tests/core/extraction/test_types.py tests/core/extraction/enrichers/__init__.py && git commit -m "feat(extraction): add cascade types layer and Enricher Protocol"`

**Checkpoint**: types.py and Enricher Protocol committed. All story phases may now proceed.

---

## Phase 3: User Story 1 — Emoji Regex Enricher (Priority: P1) 🎯 MVP

**Goal**: `EmojiRegexEnricher` extracts all `📍PlaceName`, `@PlaceName`, and location hashtag candidates from caption text — pure regex, no LLM, no HTTP.

**Independent Test**: `poetry run pytest tests/core/extraction/enrichers/test_emoji_regex.py -v`

- [x] T012 [US1] Create `src/kebi/core/extraction/enrichers/emoji_regex.py` per plan.md §Phase 3 Task 3.2 Step 3
- [x] T013 [US1] Create `tests/core/extraction/enrichers/test_emoji_regex.py` per plan.md §Phase 3 Task 3.2 Step 4
- [x] T014 [US1] Run `poetry run pytest tests/core/extraction/enrichers/test_emoji_regex.py -v`
- [x] T015 [US1] Run `poetry run mypy src/kebi/core/extraction/enrichers/emoji_regex.py --strict`
- [x] T016 [US1] Run `poetry run ruff check src/kebi/core/extraction/enrichers/emoji_regex.py`
- [x] T017 [US1] Commit: `git add src/kebi/core/extraction/enrichers/emoji_regex.py tests/core/extraction/enrichers/test_emoji_regex.py && git commit -m "feat(extraction): add EmojiRegexEnricher — emoji/hashtag candidate extraction"`

**Checkpoint**: US1 complete. EmojiRegexEnricher independently testable and committed.

---

## Phase 4: User Story 2 — LLM NER Enricher (Priority: P1)

**Goal**: `LLMNEREnricher` extracts all place names from caption via GPT-4o-mini through Instructor, with no skip guard, ADR-044 prompts, and Langfuse tracing via manual generation span.

**Independent Test**: `poetry run pytest tests/core/extraction/enrichers/test_llm_ner.py -v`

- [x] T018 [US2] Create `src/kebi/core/extraction/enrichers/llm_ner.py` per plan.md §Phase 3 Task 3.3
- [x] T019 [US2] Create `tests/core/extraction/enrichers/test_llm_ner.py` per plan.md §Phase 3 Task 3.3 (mock Instructor call)
- [x] T020 [US2] Run `poetry run pytest tests/core/extraction/enrichers/test_llm_ner.py -v`
- [x] T021 [US2] Run `poetry run mypy src/kebi/core/extraction/enrichers/llm_ner.py --strict`
- [x] T022 [US2] Run `poetry run ruff check src/kebi/core/extraction/enrichers/llm_ner.py`
- [x] T023 [US2] Commit: `git add src/kebi/core/extraction/enrichers/llm_ner.py tests/core/extraction/enrichers/test_llm_ner.py && git commit -m "feat(extraction): add LLMNEREnricher — GPT-4o-mini NER with Langfuse tracing"`

**Checkpoint**: US2 complete. LLMNEREnricher independently testable and committed.

---

## Phase 5: User Story 4 — Confidence Scoring (Priority: P2)

**Goal**: `calculate_confidence()` with multiplicative formula capped at `config.max_score` (default 0.97). `ConfidenceConfig` loaded from `app.yaml` — no float literals in the function body.

**Independent Test**: `poetry run pytest tests/core/extraction/test_confidence_new.py -v`

- [x] T024 [US4] Add `ConfidenceConfig(BaseModel)` to `src/kebi/core/config.py` and extend `ExtractionConfig` with `confidence`, `circuit_breaker_threshold`, `circuit_breaker_cooldown` per plan.md §Phase 2 Task 2.1 Step 1
- [x] T025 [US4] Add `extraction.confidence` block (with `max_score: 0.97`) and `circuit_breaker_*` fields to `config/app.yaml` per plan.md §Phase 2 Task 2.2 Step 2
- [x] T026 [US4] Add `calculate_confidence()` to `src/kebi/core/extraction/confidence.py` per plan.md §Phase 2 Task 2.3 Step 3
- [x] T027 [US4] Create `tests/core/extraction/test_confidence_new.py` per plan.md §Phase 2 Task 2.4 Step 4
- [x] T028 [US4] Run `poetry run pytest tests/core/extraction/test_confidence_new.py -v`
- [x] T029 [US4] Run `poetry run pytest tests/core/extraction/test_confidence.py -v` (regression check — existing compute_confidence must still pass)
- [x] T030 [US4] Run `poetry run mypy src/kebi/core/config.py src/kebi/core/extraction/confidence.py --strict`
- [x] T031 [US4] Run `poetry run ruff check src/kebi/core/config.py src/kebi/core/extraction/confidence.py`
- [x] T032 [US4] Commit: `git add src/kebi/core/config.py src/kebi/core/extraction/confidence.py config/app.yaml tests/core/extraction/test_confidence_new.py && git commit -m "feat(extraction): add ConfidenceConfig with max_score and calculate_confidence multiplicative formula"`

**Checkpoint**: US4 complete. Confidence scoring independently testable and committed.

---

## Phase 6: User Story 3 — Circuit Breaker + Caption Enrichers (Priority: P2)

**Goal**: `CircuitBreakerEnricher` trips on exceptions only (not `None` returns); `ParallelEnricherGroup` via `asyncio.gather`; `TikTokOEmbedEnricher` with hardcoded oEmbed URL (no config dependency); `YtDlpMetadataEnricher` via subprocess.

**Independent Test**: `poetry run pytest tests/core/extraction/test_circuit_breaker.py tests/core/extraction/enrichers/test_tiktok_oembed.py -v`

- [x] T033 [US3] Create `src/kebi/core/extraction/circuit_breaker.py` with `CircuitState`, `CircuitBreakerEnricher`, `ParallelEnricherGroup` per plan.md §Phase 4 Task 4.1
- [x] T034 [US3] Create `tests/core/extraction/test_circuit_breaker.py` per plan.md §Phase 4 Task 4.1 (tests)
- [x] T035 [US3] Run `poetry run pytest tests/core/extraction/test_circuit_breaker.py -v`
- [x] T036 [P] [US3] Create `src/kebi/core/extraction/enrichers/tiktok_oembed.py` per plan.md §Phase 4 Task 4.2 Step 2 — note: uses module-level `_TIKTOK_OEMBED_URL` and `_TIMEOUT_SECONDS` constants, no config import
- [x] T037 [P] [US3] Create `tests/core/extraction/enrichers/test_tiktok_oembed.py` per plan.md §Phase 4 Task 4.2 (mock httpx)
- [x] T038 [US3] Run `poetry run pytest tests/core/extraction/enrichers/test_tiktok_oembed.py -v`
- [x] T039 [US3] Create `src/kebi/core/extraction/enrichers/ytdlp_metadata.py` per plan.md §Phase 4 Task 4.3
- [x] T040 [US3] Run `poetry run mypy src/kebi/core/extraction/circuit_breaker.py src/kebi/core/extraction/enrichers/tiktok_oembed.py src/kebi/core/extraction/enrichers/ytdlp_metadata.py --strict`
- [x] T041 [US3] Run `poetry run ruff check src/kebi/core/extraction/circuit_breaker.py src/kebi/core/extraction/enrichers/tiktok_oembed.py src/kebi/core/extraction/enrichers/ytdlp_metadata.py`
- [x] T042 [US3] Commit: `git add src/kebi/core/extraction/circuit_breaker.py src/kebi/core/extraction/enrichers/tiktok_oembed.py src/kebi/core/extraction/enrichers/ytdlp_metadata.py tests/core/extraction/test_circuit_breaker.py tests/core/extraction/enrichers/test_tiktok_oembed.py && git commit -m "feat(extraction): add CircuitBreakerEnricher, ParallelEnricherGroup, TikTokOEmbedEnricher, YtDlpMetadataEnricher"`

**Checkpoint**: US3 complete. All four user stories implemented and committed.

---

## Phase 7: Polish & Full Verification

**Purpose**: Confirm zero regressions across the entire run before closing the branch.

- [x] T043 Run full test suite: `poetry run pytest --tb=short -q`
- [x] T044 [P] Run `poetry run mypy src/` — zero errors expected
- [x] T045 [P] Run `poetry run ruff check src/ tests/` — zero violations expected

---

## Dependencies & Execution Order

### Phase Dependencies

```
Phase 1 (Setup)
    └── Phase 2 (Foundational) ← BLOCKS all stories
              ├── Phase 3 (US1 EmojiRegex)   ← independent
              ├── Phase 4 (US2 LLM NER)      ← independent
              ├── Phase 5 (US4 Confidence)   ← independent
              └── Phase 6 (US3 Circuit)      ← independent of US1/US2/US4
                        └── Phase 7 (Polish)
```

- **US1, US2, US4** are fully independent after Phase 2 — can proceed in any order or in parallel
- **US3** only needs Phase 2 (types + Enricher Protocol); `CircuitBreakerEnricher` wraps any `Enricher`, no dependency on specific enrichers

### Within Each Phase

- Implementation task → then verification tasks → then commit
- T036 and T037 are `[P]`: `tiktok_oembed.py` and `test_tiktok_oembed.py` are different files with no dependency between them

### Parallel Opportunities

Once Phase 2 completes, all three of Phase 3/4/5 can start simultaneously:

```bash
; Three independent streams after T011
Stream A: T012 → T013 → T014 → T015 → T016 → T017  (US1)
Stream B: T018 → T019 → T020 → T021 → T022 → T023  (US2)
Stream C: T024 → T025 → T026 → T027 → T028 → T029 → T030 → T031 → T032  (US4)
; Then Phase 6 (US3): T033 → T034 → T035 → T036+T037 → T038 → T039 → T040 → T041 → T042
; Then Phase 7: T043 → T044+T045
```

---

## Implementation Strategy

### MVP (User Story 1 only)

1. Phase 1 + Phase 2 (Foundational)
2. Phase 3 (US1 — EmojiRegexEnricher)
3. Validate: `poetry run pytest tests/core/extraction/ -v`
4. Stop and assess — emoji extraction works end-to-end with zero external dependencies

### Full Run 1

Complete phases in order: 1 → 2 → 3/4/5 (any order or parallel) → 6 → 7.
Each phase produces a committed, verified increment. Existing pipeline untouched throughout.

### What is NOT built in this run

- `EnrichmentPipeline` (orchestrates enrichers in sequence) — Run 2
- `dedup_candidates()` — Run 2
- `SubtitleCheckEnricher`, `WhisperAudioEnricher`, `VisionFramesEnricher` — Run 2
- `GooglePlacesValidator` — Run 2
- Background dispatch and `dispatch_background()` — Run 3
- `ExtractionService` rewrite — Run 3
- API schema changes (`/v1/extract-place` still returns single result) — Run 3
- Deletion of legacy `ExtractionResult(BaseModel)` in `result.py` — Run 3

---

## Notes

- All new files must pass `mypy --strict` — verify after each phase, not just at the end
- `asyncio_mode = "auto"` in pytest config — no `@pytest.mark.asyncio` needed
- `LLMNEREnricher` uses `get_langfuse_client()` + manual span (see research.md R-001) — `get_langfuse_handler()` does not exist
- `ConfidenceConfig.base_scores` uses `dict[str, float]` with `.value` key lookup (see research.md R-003)
- `ConfidenceConfig.max_score = 0.97` — system never claims perfect certainty; `min(..., 1.0)` is NOT correct
- `TikTokOEmbedEnricher` uses module-level constants `_TIKTOK_OEMBED_URL` and `_TIMEOUT_SECONDS = 10.0` — no `get_config()` import
- Git comment char is `;` not `#` in commit messages
