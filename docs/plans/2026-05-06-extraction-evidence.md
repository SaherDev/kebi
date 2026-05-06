# Extraction Evidence Trail

## Goal

Replace `CandidatePlace.source: ExtractionLevel` + `corroborated: bool` + `signals: list[str]` with a single richer structure: a per-candidate **evidence trail** that records every producer that contributed, the medium where the evidence lived, and (when available) the actual snippet of text or visual identifier. Same shape persists onto `ValidatedCandidate` and gets written alongside the saved `Place` so we can answer, after the fact: *what did the pipeline actually see that led us to save this place?*

The current `ExtractionLevel` enum is the pinch point — it tries to encode "who emitted this" in one value, but the real picture is multi-axis (producer + medium + content) and multi-valued (a name can be backed by N pieces of evidence).

## Why now

- Today's pipeline funnels vision/Google-Maps names through `known_places` → `LLM_NER`. The producer of the name is silently dropped on the way through. Every candidate looks like `[LLM_NER]`.
- Whisper / Subtitle both write to `context.transcript`; TikTok-oEmbed / yt-dlp both write to `context.caption`. By the time NER reads the text, the producer of those bytes is also gone.
- The frontend already has copy for `provenance` (`apps/web/messages/en.json`); the API has no data to fill it.
- Confidence weighting depends on producer identity (`config.base_scores[level.value]`). With every candidate degraded to `LLM_NER`, every other base score is unreachable.

## Decisions

- **Two enums, two axes.**
  - `Producer` (rename of `ExtractionLevel`) — *who* contributed evidence. Includes text producers (`TIKTOK_OEMBED`, `YTDLP_METADATA`, `WHISPER_AUDIO`, `SUBTITLE_CHECK`, `PHOTO_DETECTOR`) and name producers (`LLM_NER`, `GOOGLE_MAPS_LIST`, `VISION_FRAMES`, `VISION_IMAGES`). Drop `EMOJI_REGEX` (dead).
  - `Medium` — *where* the evidence lived in pipeline state. `CAPTION`, `TRANSCRIPT`, `TITLE`, `HASHTAG`, `LOCATION_TAG`, `EMOJI_MARKER`, `SUPPLEMENTARY_TEXT`, `FRAME` (vision frame), `IMAGE` (photo carousel slide), `LIST` (Google Maps list entry).
- **`Evidence` is a frozen dataclass** carrying `producer + medium + snippet + metadata`. Equality is by `(producer, medium, snippet)` so dedup-merge has clean semantics.
- **`CandidatePlace.evidence: list[Evidence]`** replaces `source` + `corroborated` + `signals`. `ValidatedCandidate.evidence` mirrors it.
- **Text producers tag their writes.** Each enricher that writes to `context.caption` / `context.transcript` / etc. also appends a single `Evidence` entry to a new `context.text_evidence: list[Evidence]` list. NER reads this list when emitting candidates and stamps the matching `Evidence` items onto the candidate's `evidence`. This is how Whisper-vs-Subtitle and TikTok-vs-yt-dlp distinction survives.
- **Name producers (vision, Google Maps list) write `KnownPlace`** (already a dataclass holding `name + producer`) into `context.known_places: list[KnownPlace]`. NER joins on normalized name and stamps the matching producer onto the candidate's evidence as `Evidence(producer=GOOGLE_MAPS_LIST, medium=LIST, snippet=name)` etc.
- **Snippets are the actual content** — not synthetic strings. Caption snippet = the caption text (truncated to ~200 chars). Frame snippet = the OCR-ish line the vision LLM emitted (or the frame index if we don't have it). Audio transcript snippet = the transcript line the name appears on (regex window). Best-effort; `None` when we don't have it cheaply.
- **Confidence formula keys on `evidence`**: `base = max(per-producer score, per-medium score)` over all evidence items; `bonus = corroboration_bonus` when there's more than one distinct `(producer, medium)` pair. The existing `signal_scores` map carries forward as `medium_scores`; the existing `base_scores` map carries forward as `producer_scores`. One config rename, no behavioral diff for the trivial single-evidence case.
- **Dedup merges evidence lists** by union (preserving first-seen order). The pre-validation winner pick "by lowest level" goes away — there's no single winner level any more.
- **No `place_evidence` table in this plan.** In-memory + Langfuse-traced + emitted on the API response. DB persistence is a follow-up: defer until we've used the in-memory trail in real recommendations and know what queries we want.
- **API: `ExtractPlaceItem` gains an `evidence: list[EvidenceDTO]` field.** Optional on the wire for one release while we finish migrating callers.
- **`ExtractionLevel` is renamed to `Producer` in code** but retains the same `value` strings for the values that survive (`llm_ner`, `google_maps_list`, `vision_frames`). The `config/app.yaml` keys under `extraction.confidence.base_scores` keep their string identity so production config doesn't break. New keys added: `vision_images`, `whisper_audio`, `subtitle_check`, `tiktok_oembed`, `ytdlp_metadata`, `photo_detector`. `emoji_regex` removed.

## Out of scope (deferred)

- `place_evidence` Postgres table. The in-memory trail flows; persistence comes later.
- UI rendering of the trail (frontend repo concern).
- Per-evidence confidence (for now confidence is candidate-level; `Evidence` items don't carry their own score).
- `_LEVEL_ORDER` priority config in YAML. With evidence-driven confidence the dedup tiebreaker disappears; first-seen order is fine.

## Shape

```python
class Producer(Enum):
    # Name producers — emit candidate names
    LLM_NER = "llm_ner"
    GOOGLE_MAPS_LIST = "google_maps_list"
    VISION_FRAMES = "vision_frames"
    VISION_IMAGES = "vision_images"
    # Text producers — populate caption / transcript / etc.
    TIKTOK_OEMBED = "tiktok_oembed"
    YTDLP_METADATA = "ytdlp_metadata"
    WHISPER_AUDIO = "whisper_audio"
    SUBTITLE_CHECK = "subtitle_check"
    PHOTO_DETECTOR = "photo_detector"


class Medium(Enum):
    CAPTION = "caption"
    SUPPLEMENTARY_TEXT = "supplementary_text"
    TRANSCRIPT = "transcript"
    TITLE = "title"
    HASHTAG = "hashtag"
    LOCATION_TAG = "location_tag"
    EMOJI_MARKER = "emoji_marker"
    FRAME = "frame"        # video frame
    IMAGE = "image"        # photo carousel slide
    LIST = "list"          # Google Maps list entry


@dataclass(frozen=True)
class Evidence:
    producer: Producer
    medium: Medium
    snippet: str | None = None
    metadata: tuple[tuple[str, str | int | float | bool], ...] = ()


@dataclass
class KnownPlace:
    name: str
    producer: Producer
    medium: Medium = Medium.LIST       # default for Google Maps list; vision overrides
    snippet: str | None = None


@dataclass
class CandidatePlace:
    place_name: str
    place_type: PlaceType
    evidence: list[Evidence]
    subcategory: str | None = None
    tags: list[str] = field(default_factory=list)
    attributes: PlaceAttributes = field(default_factory=PlaceAttributes)


@dataclass
class ValidatedCandidate:
    place_name: str
    place_type: PlaceType
    provider: PlaceProvider
    external_id: str
    confidence: float
    evidence: list[Evidence]
    subcategory: str | None = None
    tags: list[str] = field(default_factory=list)
    attributes: PlaceAttributes = field(default_factory=PlaceAttributes)
    match_lat: float | None = None
    match_lng: float | None = None
    match_address: str | None = None


@dataclass
class ExtractionContext:
    # ... existing fields ...
    known_places: list[KnownPlace] = field(default_factory=list)
    text_evidence: list[Evidence] = field(default_factory=list)  # NEW
```

## Per-producer evidence contract

Every enricher that mutates `ExtractionContext` MUST record its contribution. The pipeline is only useful if the trail is complete; a producer that writes data but skips evidence appears nowhere downstream and silently distorts confidence.

The rules:

1. **Write-side rule** — when an enricher actually writes pipeline state (populates a field that was previously `None` / empty), it appends an `Evidence` (text producers) or `KnownPlace` (name producers) entry.
2. **No-op rule** — when an enricher skips because state is already populated (first-write-wins), or because of a source / `is_photo_post` guard, it appends nothing. Evidence reflects what the pipeline actually saw, not what enrichers attempted.
3. **Snippet rule** — when the enricher has the actual content (caption text, transcript line, name string), include it truncated to 200 chars. When it doesn't (e.g. `PhotoDetectorEnricher` only writes flags), `snippet=None` and put a useful detail in `metadata`.
4. **Idempotency** — re-running an enricher (e.g. circuit-breaker probe) does NOT re-append evidence. Enrichers check the no-op condition first.

### Text producers (append to `context.text_evidence`)

| Producer file | Producer enum | Evidence written | Snippet |
|---|---|---|---|
| `tiktok_oembed.py` | `TIKTOK_OEMBED` | one `(TIKTOK_OEMBED, CAPTION)` when caption written | `caption[:200]` |
| `ytdlp_metadata.py` | `YTDLP_METADATA` | one entry per field it populated: `(_, CAPTION)` when caption written, `(_, TITLE)` when title written, one `(_, HASHTAG)` per hashtag (or one with the joined list as snippet), `(_, LOCATION_TAG)` when location_tag written | per field — caption/title text, hashtag string, location string |
| `whisper_audio.py` | `WHISPER_AUDIO` | one `(WHISPER_AUDIO, TRANSCRIPT)` when transcript written | `transcript[:200]` |
| `subtitle_check.py` | `SUBTITLE_CHECK` | one `(SUBTITLE_CHECK, TRANSCRIPT)` when transcript written | `transcript[:200]` |
| `photo_detector.py` | `PHOTO_DETECTOR` | one `(PHOTO_DETECTOR, IMAGE)` when `is_photo_post=True` is set | `None`; `metadata={"image_count": N}` |

Note: when `WhisperAudioEnricher` runs but `SubtitleCheckEnricher` already wrote the transcript, Whisper's own no-op check (`context.transcript is not None`) fires — and Whisper appends nothing. That's correct: only Subtitle's evidence appears, because Subtitle's text is what NER actually read.

### Name producers (append to `context.known_places`)

| Producer file | Producer enum | KnownPlace fields | Snippet |
|---|---|---|---|
| `vision_frames.py` | `VISION_FRAMES` | `KnownPlace(name, VISION_FRAMES, FRAME)` | the extracted name itself |
| `vision_images.py` | `VISION_IMAGES` | `KnownPlace(name, VISION_IMAGES, IMAGE)` | the extracted name itself |
| `google_maps_list.py` | `GOOGLE_MAPS_LIST` | `KnownPlace(name, GOOGLE_MAPS_LIST, LIST)` | the list-item name |

When NER emits a candidate, it scans `known_places` and stamps every matching `(producer, medium, snippet)` triple onto the candidate's `evidence`.

### LLM_NER itself

`LLMNEREnricher` is also a producer — when it emits a candidate from text, it appends one `Evidence(LLM_NER, medium=X)` for each text field that contains the candidate name (normalized match):

| Text field on context | Medium | Snippet rule |
|---|---|---|
| `caption` (or `supplementary_text`) | `CAPTION` / `SUPPLEMENTARY_TEXT` | the full caption / supplementary_text, truncated to 200 chars |
| `transcript` | `TRANSCRIPT` | a 200-char window around the matched name (first occurrence) |
| `title` | `TITLE` | the title string |
| `location_tag` | `LOCATION_TAG` | the location_tag string |
| caption matches the emoji-marker regex `^[📍📌🗺]` | `EMOJI_MARKER` | the matched line |
| hashtag matches the candidate name | `HASHTAG` | the hashtag string |

So a candidate's full `evidence` after NER runs is:
- One or more `LLM_NER` items (one per text field where the name was found, plus emoji/hashtag if applicable)
- All `text_evidence` items whose source field contains the candidate name (so `YTDLP_METADATA + CAPTION` rides alongside `LLM_NER + CAPTION`)
- All `known_places` items whose name matches (so `VISION_FRAMES + FRAME`, `GOOGLE_MAPS_LIST + LIST`, etc.)

### Invariant

A candidate's evidence list is **non-empty** by construction — `LLMNEREnricher` is the only emitter of candidates, and it always stamps at least one `Evidence(LLM_NER, ...)` (otherwise the candidate's name wouldn't be in any text source and the LLM wouldn't have produced it). Validation MAY check `len(candidate.evidence) >= 1` as a safety net; production code shouldn't need it, but the assertion catches regressions.

## Changes by file

### `src/totoro_ai/core/extraction/types.py`
- Rename `ExtractionLevel` → `Producer`. Update value list. Drop `EMOJI_REGEX`. Add the missing producers.
- Add `Medium` enum.
- Add `Evidence` frozen dataclass.
- Update `KnownPlace` to carry `medium` + optional `snippet`.
- Replace `CandidatePlace.source` / `corroborated` / `signals` with `evidence: list[Evidence]`.
- Replace `ValidatedCandidate.resolved_by` / `corroborated` with `evidence: list[Evidence]`.
- Add `ExtractionContext.text_evidence: list[Evidence]`.

### Producer file edits — see *Per-producer evidence contract* table above for the exact write each one makes.

### `src/totoro_ai/core/extraction/enrichers/llm_ner.py`
- Build `evidence` for each emitted candidate by joining:
  - One `Evidence(LLM_NER, <medium>)` for each text source the LLM had access to that contains the candidate name (regex match against caption / transcript / supplementary_text / title — using a normalized check).
  - All `text_evidence` entries that contain the candidate name (so YTDLP_METADATA + LLM_NER both appear when a name was in the caption).
  - All `known_places` entries whose name matches (so VISION_FRAMES, GOOGLE_MAPS_LIST, etc. surface).
- Drop the `_NERPlace.signals: list[str]` field. The LLM no longer self-reports signals — we infer medium from text content. (Tradeoff: lose `emoji_marker` self-reporting; recover it via regex on the caption.)

### `src/totoro_ai/core/extraction/validator.py`
- Pass `candidate.evidence` to `calculate_confidence`.
- Construct `ValidatedCandidate(evidence=list(candidate.evidence), ...)`.
- Drop `_lookup_city`'s implicit dependency — already on `candidate.attributes.location_context`.

### `src/totoro_ai/core/extraction/confidence.py`
- New signature: `calculate_confidence(evidence: list[Evidence], match_modifier, config) -> float`.
- Base = `max(producer_scores[e.producer.value] for e in evidence ∪ medium_scores[e.medium.value] for e in evidence)`.
- Bonus = `corroboration_bonus` when `len({(e.producer, e.medium) for e in evidence}) > 1`, else 0.
- Cap at `config.max_score`.
- Rename `config.base_scores` → `config.producer_scores`; rename `config.signal_scores` → `config.medium_scores`. Update `config/app.yaml` accordingly.

### `src/totoro_ai/core/extraction/dedup.py`
- `dedup_candidates`: group by normalized name, merge evidence lists by union (preserving order), merge attributes.
- `dedup_validated_by_provider_id`: group by provider_id, merge evidence + attributes, take max confidence, apply corroboration bonus when merged evidence has 2+ distinct `(producer, medium)` pairs.
- Drop `_LEVEL_ORDER` and the lowest-index winner pick.

### `src/totoro_ai/core/extraction/persistence.py`
- `_to_place_create`: unchanged — evidence isn't on `PlaceCreate`.
- (Future) write evidence to the persistence side. Out of scope for this plan.

### `src/totoro_ai/api/schemas/extract_place.py`
- Add `EvidenceDTO` Pydantic model mirroring `Evidence`.
- `ExtractPlaceItem.evidence: list[EvidenceDTO] = []` — optional with empty default for one release.

### `src/totoro_ai/core/extraction/service.py`
- `_outcome_to_item_dict` adds `"evidence": [e.to_dto() for e in outcome.metadata.evidence]`.

### `config/app.yaml`
- Rename `extraction.confidence.base_scores` → `extraction.confidence.producer_scores`. Add new keys for the producers we just added. Drop `emoji_regex`.
- Rename `extraction.confidence.signal_scores` → `extraction.confidence.medium_scores`. Re-key the existing values: `caption` → unchanged, `hashtag` → unchanged, `emoji_marker` → unchanged, `location_tag` → unchanged. Add `transcript`, `title`, `frame`, `image`, `list`, `supplementary_text`.

### `src/totoro_ai/core/config.py`
- Update `ConfidenceConfig` field names accordingly.

## Coverage check (one row per producer)

The PR is not done until each producer has a test that asserts:
- it appends evidence on the write path,
- it appends nothing on the no-op path,
- the snippet content matches the rule above.

| Producer | Test file | Write-path test | No-op test |
|---|---|---|---|
| `LLM_NER` | `test_llm_ner.py` | candidate evidence includes `(LLM_NER, CAPTION)` when caption contains name | candidate evidence does NOT include `(LLM_NER, TRANSCRIPT)` when name is absent from transcript |
| `GOOGLE_MAPS_LIST` | `test_google_maps_list.py` | each scraped item appended as `KnownPlace(GOOGLE_MAPS_LIST, LIST)` | empty list response → no `known_places` writes |
| `VISION_FRAMES` | `test_vision_frames.py` (new) | each name appended as `KnownPlace(VISION_FRAMES, FRAME)` | `is_photo_post=True` → no writes |
| `VISION_IMAGES` | `test_vision_images.py` | each name appended as `KnownPlace(VISION_IMAGES, IMAGE)` | non-photo-post → no writes |
| `TIKTOK_OEMBED` | `test_tiktok_oembed.py` | one `(TIKTOK_OEMBED, CAPTION)` written when caption populated | caption already set → no write |
| `YTDLP_METADATA` | `test_ytdlp_metadata.py` | one entry per populated field (caption / title / hashtag / location_tag) | each field already set → that field's entry skipped |
| `WHISPER_AUDIO` | `test_whisper_audio.py` (new) | one `(WHISPER_AUDIO, TRANSCRIPT)` when transcript written | transcript already set OR `is_photo_post=True` → no write |
| `SUBTITLE_CHECK` | `test_subtitle_check.py` (new) | one `(SUBTITLE_CHECK, TRANSCRIPT)` when transcript written | transcript already set OR `is_photo_post=True` → no write |
| `PHOTO_DETECTOR` | `test_photo_detector.py` | one `(PHOTO_DETECTOR, IMAGE, metadata={image_count: N})` when photo post detected | non-photo-post → no write |

## Tests

- `test_types.py` — update construction; assert `evidence` shape.
- `test_dedup.py` — assert merged evidence after dedup.
- `test_validator.py` — assert evidence passed through; assert confidence formula with multi-evidence case.
- `test_persistence.py` — construct `ValidatedCandidate(evidence=...)`.
- `test_service.py` — assert `evidence` appears on `ExtractPlaceItem`.
- `test_extraction_pipeline.py` — assert evidence flows end-to-end.
- `test_llm_ner.py` — new tests for evidence stamping (text matches → `LLM_NER` + matching text producer; known_places matches → name producer).
- `test_vision_frames.py` / `test_vision_images.py` / `test_google_maps_list.py` (new) — assert `KnownPlace` shape with full evidence fields.
- `test_tiktok_oembed.py` / `test_ytdlp_metadata.py` / `test_whisper_audio.py` / `test_subtitle_check.py` (some new) — assert `text_evidence` is appended.
- `test_confidence.py` — rewrite around new signature.

## Verification

```bash
poetry run ruff check src/ tests/
poetry run mypy src/
poetry run pytest tests/core/extraction/ -x
poetry run pytest                      # full suite
```

Manual smoke test:
- TikTok video URL with caption + transcript + on-screen text — expect candidate evidence to include `[LLM_NER+CAPTION, YTDLP_METADATA+CAPTION, LLM_NER+TRANSCRIPT, WHISPER_AUDIO+TRANSCRIPT, VISION_FRAMES+FRAME]` for a name that hits all sources.
- Instagram photo carousel — expect `[VISION_IMAGES+IMAGE, LLM_NER+CAPTION, YTDLP_METADATA+CAPTION]`.
- Google Maps shared list — expect `[GOOGLE_MAPS_LIST+LIST, LLM_NER+...]` for each scraped item.
- Plain text "fuji ramen, joe's pizza" — expect `[LLM_NER+SUPPLEMENTARY_TEXT]` for each.

In Langfuse: each save trace should now show the full evidence list per candidate, not just `source=LLM_NER` for everything.

## Risk / Tradeoffs

- **Big-touch refactor.** Touches every enricher + validator + dedup + confidence + persistence + service + tests. ~15 source files, ~20 test files. One PR, one diff, but a heavy review.
- **`signals` self-reporting goes away.** The LLM previously emitted `["emoji_marker", "caption"]` directly. We replace with regex-based inference. Risk: regex misses edge cases the LLM caught (e.g., emojis used as visual punctuation that aren't real markers). Mitigation: keep `emoji_marker` as a `Medium` value and detect via a focused regex on the caption text (📍, 📌, etc.). If accuracy drops measurably we can re-add a self-report field later.
- **Snippet inflation.** Storing a snippet per evidence item bloats in-memory state and (eventually) DB rows. Cap at 200 chars. Defer DB persistence anyway.
- **Config rename is breaking.** Production `app.yaml` will need updated keys. Plan to keep a compatibility shim in `ConfidenceConfig` for one release that accepts both old and new key names.

## Sequencing

1. Land `types.py` + `confidence.py` + `config.py` + `app.yaml` in one logical change. Tests for these.
2. Update name producers (`vision_frames`, `vision_images`, `google_maps_list`) to write `KnownPlace` with full fields.
3. Update text producers (`tiktok_oembed`, `ytdlp_metadata`, `whisper_audio`, `subtitle_check`, `photo_detector`) to append `text_evidence`.
4. Rewrite `llm_ner.py` to build evidence by joining text + known_places against emitted names.
5. Update `validator.py` + `dedup.py` to use the new shape.
6. Update API schema + service to surface evidence on the response.
7. Update tests across the board.
8. Run full suite, ruff, mypy.

One PR. Reviewable in one sitting if the diff is well-organized; otherwise consider splitting at step 3/4.
