# Data Model: Extraction Cascade Foundation — Phases 1–4

**Branch**: `010-extraction-cascade-run1` | **Date**: 2026-04-06

This run is purely additive. No database schema changes. No new tables, no migrations. All entities below are in-memory types used within the extraction pipeline.

---

## New types (all in `src/kebi/core/extraction/types.py`)

### `ExtractionLevel` (Enum)

Identifies which enricher level produced a candidate. Used as the key into `ConfidenceConfig.base_scores` (via `.value` string lookup).

| Value | String | Base confidence |
|-------|--------|----------------|
| `EMOJI_REGEX` | `"emoji_regex"` | 0.95 |
| `LLM_NER` | `"llm_ner"` | 0.80 |
| `SUBTITLE_CHECK` | `"subtitle_check"` | 0.75 |
| `WHISPER_AUDIO` | `"whisper_audio"` | 0.65 |
| `VISION_FRAMES` | `"vision_frames"` | 0.55 |

Only levels that produce `CandidatePlace` objects are in this enum. Caption enrichers (oEmbed, yt-dlp) and the validator (Google Places) are not included — they never create candidates directly.

---

### `CandidatePlace` (dataclass)

An unvalidated place name with enricher provenance. The unit flowing between enrichers and the validator.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `name` | `str` | Yes | Place name as extracted |
| `city` | `str \| None` | Yes | City hint from hashtag or NER |
| `cuisine` | `str \| None` | Yes | Cuisine hint from NER; None for regex |
| `source` | `ExtractionLevel` | Yes | Which enricher found this |
| `corroborated` | `bool` | No | Default False; set True by dedup (Run 2) |

**Invariants**: `name` is never empty. `source` is always set at construction. `corroborated` is only set to True by `dedup_candidates()` (implemented in Run 2).

---

### `ExtractionContext` (dataclass)

Shared mutable state threaded through all enrichers in Phase 1 (Enrichment). Enrichers write into it; they never read results out of it — that's the validator's job.

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `url` | `str \| None` | Required | TikTok/Instagram URL or None for plain text |
| `user_id` | `str` | Required | User ID from the HTTP request |
| `supplementary_text` | `str` | `""` | Text extracted by `parse_input()` |
| `caption` | `str \| None` | `None` | Set by caption enrichers (first-write-wins) |
| `transcript` | `str \| None` | `None` | Set by `SubtitleCheckEnricher` (Run 2); causes `WhisperAudioEnricher` to skip |
| `candidates` | `list[CandidatePlace]` | `[]` | Populated by candidate enrichers |
| `pending_levels` | `list[ExtractionLevel]` | `[]` | Set by `dispatch_background()` (Run 2) |

**Mutation rules**: `caption` and `transcript` are first-write-wins — once set, no enricher may overwrite them. `candidates` is append-only during enrichment. `pending_levels` is set once by `dispatch_background()`.

---

### `ExtractionResult` (dataclass)

A validated, scored result from `GooglePlacesValidator`. The unit returned from the pipeline to the service layer. One per validated candidate.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `place_name` | `str` | Yes | Canonical name from Google Places |
| `address` | `str \| None` | Yes | Formatted address |
| `city` | `str \| None` | Yes | City from candidate or address parsing |
| `cuisine` | `str \| None` | Yes | From NER candidate |
| `confidence` | `float` | Yes | Multiplicative score from `calculate_confidence()` |
| `resolved_by` | `ExtractionLevel` | Yes | Level that found the candidate |
| `corroborated` | `bool` | Yes | Whether two sources agreed |
| `external_provider` | `str \| None` | Yes | `"google"` when validated |
| `external_id` | `str \| None` | Yes | Google Places place_id |

**Note**: This type has the same name as the existing `ExtractionResult(BaseModel)` in `result.py`. They coexist until Run 3. Import disambiguation: code using the new type imports from `kebi.core.extraction.types`; code using the old type imports from `kebi.core.extraction.result`. No file in this run imports the old type.

---

### `ProvisionalResponse` (dataclass)

Returned when Phase 2 validation finds nothing and Phase 3 background dispatch fires.

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `extraction_status` | `str` | Required | `"processing"` |
| `confidence` | `float` | Required | `0.0` (no validated result yet) |
| `message` | `str` | Required | Human-readable status |
| `pending_levels` | `list[ExtractionLevel]` | `[]` | Levels queued for background |

---

### `ExtractionPending` (dataclass)

Typed domain event dispatched when Phase 3 fires. Carries all context needed by the background handler.

| Field | Type | Notes |
|-------|------|-------|
| `user_id` | `str` | User ID |
| `url` | `str \| None` | Source URL for background enrichers |
| `pending_levels` | `list[ExtractionLevel]` | Levels to run in background |
| `context` | `ExtractionContext` | Full context including any caption already fetched |

---

## Modified config type (in `src/kebi/core/config.py`)

### `ConfidenceConfig` (new Pydantic BaseModel)

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `base_scores` | `dict[str, float]` | See below | Keys are `ExtractionLevel.value` strings |
| `corroboration_bonus` | `float` | `0.10` | Added when `corroborated=True` |
| `max_score` | `float` | `0.97` | Hard ceiling on all confidence scores; 1.0 implies certainty the system has not earned |

Default `base_scores`:
```python
{"emoji_regex": 0.95, "llm_ner": 0.80, "subtitle_check": 0.75, "whisper_audio": 0.65, "vision_frames": 0.55}
```

### `ExtractionConfig` additions

Three new fields added to existing `ExtractionConfig(BaseModel)`:

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `confidence` | `ConfidenceConfig` | Default `ConfidenceConfig` | New confidence config |
| `circuit_breaker_threshold` | `int` | `5` | Failures before circuit opens |
| `circuit_breaker_cooldown` | `float` | `900.0` | Seconds before half-open probe |

Existing fields (`confidence_weights`, `thresholds`, `mutable_fields`) are unchanged.

---

## No DB changes

This run touches zero database tables. No Alembic migration required. The `places`, `embeddings`, `taste_model`, `interaction_log` schemas are untouched.
