# Research: Extraction Cascade Foundation — Phases 1–4

**Branch**: `010-extraction-cascade-run1` | **Date**: 2026-04-06

---

## R-001: Langfuse tracing — `get_langfuse_handler()` does not exist

**Assumption in spec**: `LLMNEREnricher` attaches Langfuse via `get_langfuse_handler()`

**Finding**: `src/kebi/providers/tracing.py` exposes only `get_langfuse_client() -> Any | None`. There is no `get_langfuse_handler()` function. The existing `InstructorClient.extract()` in `providers/llm.py` does not attach any Langfuse tracing — tracing is not yet wired for structured extraction calls.

**Decision**: `LLMNEREnricher` uses `get_langfuse_client()` to manually create a Langfuse generation span around the `InstructorClient.extract()` call. Pattern:
```python
client = get_langfuse_client()
generation = client.generation(...) if client else None
try:
    result = await self._instructor_client.extract(...)
    if generation: generation.end(output=...)
finally:
    if generation and not generation.end_time: generation.end()
```
If `get_langfuse_client()` returns None (Langfuse not configured), the enricher proceeds without tracing — consistent with the existing graceful-degradation pattern in `tracing.py`.

**Alternatives considered**: Skip tracing entirely for this run (deferred). Rejected — ADR-025 is a binding constraint and `LLMNEREnricher` makes a real LLM call that must be observable in production.

---

## R-002: `LLMNEREnricher` constructor — single client, not two

**Assumption in spec**: `LLMNEREnricher` takes both `llm` (`LLMClientProtocol`) and `instructor_client` (`InstructorClient`)

**Finding**: `InstructorClient` (in `providers/llm.py`) already wraps the OpenAI async client and handles structured extraction. `LLMClientProtocol` provides `complete()` / `stream()` — unstructured text. `LLMNEREnricher` needs structured Pydantic output (list of places), so it uses Instructor exclusively. There is no need for a separate `llm: LLMClientProtocol` constructor argument.

**Decision**: `LLMNEREnricher.__init__` takes only `instructor_client: InstructorClient`. Instantiated via `get_instructor_client("intent_parser")` at wiring time in `deps.py` (Run 3). No separate `llm` arg.

---

## R-003: `ConfidenceConfig` — BaseModel, not dataclass; string-keyed base_scores

**Assumption in spec**: `ConfidenceConfig` as Python `@dataclass` with `base_scores: dict[ExtractionLevel, float]`

**Finding**: `ExtractionConfig` is a Pydantic `BaseModel`. All other config types in `config.py` are Pydantic models. Embedding a plain Python dataclass in a Pydantic model is supported in Pydantic v2 but adds friction. More importantly, `dict[ExtractionLevel, float]` as a Pydantic field with enum keys requires special handling — Pydantic serializes enum keys to their values in JSON/YAML by default, but loading from YAML (where keys are strings) would require a custom validator.

**Decision**: `ConfidenceConfig` is a `BaseModel` with `base_scores: dict[str, float]` (string keys matching `ExtractionLevel.value`). `calculate_confidence()` looks up via `source.value`:
```python
base = config.base_scores.get(source.value, 0.50)
```
This requires no custom loading. YAML keys (`emoji_regex`, `llm_ner`, etc.) map directly to `ExtractionLevel.value` strings. No circular imports — `config.py` does not need to import `ExtractionLevel` at all.

**Alternatives considered**: Python dataclass with `dict[ExtractionLevel, float]` and a `model_validator` to convert string keys at load time. Rejected — adds complexity for no benefit, inconsistent with the rest of config.py.

---

## R-004: `ExtractionConfig` new fields — defaults required

**Finding**: `ExtractionConfig(BaseModel)` in `config.py` is loaded from `app.yaml`. New fields without defaults will cause a Pydantic `ValidationError` at startup if `app.yaml` is not updated simultaneously. Since `app.yaml` is updated in the same commit as `config.py`, this is safe — but as a defensive measure, fields should carry defaults.

**Decision**: New fields in `ExtractionConfig`:
```python
circuit_breaker_threshold: int = 5
circuit_breaker_cooldown: float = 900.0
confidence: ConfidenceConfig = ConfidenceConfig(
    base_scores={
        "emoji_regex": 0.95,
        "llm_ner": 0.80,
        "subtitle_check": 0.75,
        "whisper_audio": 0.65,
        "vision_frames": 0.55,
    },
    corroboration_bonus=0.10,
)
```
Default values match `app.yaml` values. If `app.yaml` is present (it always is), the config file values override the defaults. Startup never fails due to missing fields.

---

## R-005: `tests/core/extraction/enrichers/` — `__init__.py` required

**Finding**: The existing `tests/core/extraction/__init__.py` exists. For pytest to discover tests under `tests/core/extraction/enrichers/`, an `__init__.py` is required in that directory (project uses src layout with `asyncio_mode = "auto"` in pytest config).

**Decision**: Create `tests/core/extraction/enrichers/__init__.py` (empty) as part of Phase 3.

---

## R-006: `EmojiRegexEnricher` — regex patterns for `📍`, `@`, and hashtag locations

**Decision**: Three regex patterns run in order on the same text:
1. `📍([^📍@#\n]+)` — captures text immediately after 📍 emoji
2. `@([A-Za-z0-9_]+)` — captures @mentions as candidate place names (already tagged by creator)
3. `#([A-Za-z]+)` — captures city/location hashtags; used for `city` field if it matches a known short word pattern; NOT added as a separate candidate (just enriches existing candidates' `city` field)

The `city` from hashtag is extracted only when the hashtag appears near a `📍` or `@` candidate in the same text segment. Simple heuristic: if a hashtag appears in the same caption as a `📍` match, the first standalone single-word hashtag (not a brand or food tag) is used as `city`.

In practice, the regex for Phase 3 keeps it simple: capture `📍PlaceName` and `@PlaceName` as candidates. Hashtag-to-city extraction is a refinement that can be added in a follow-up without changing the interface.

---

## R-007: `YtDlpMetadataEnricher` subprocess — asyncio pattern

**Decision**: Use `asyncio.create_subprocess_exec` for non-blocking subprocess execution:
```python
proc = await asyncio.create_subprocess_exec(
    "yt-dlp", "--dump-json", url,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)
stdout, _ = await proc.communicate()
```
Raises `RuntimeError` on non-zero return code (propagates to `CircuitBreakerEnricher`). Does NOT catch `FileNotFoundError` (yt-dlp not installed) — this would trip the circuit breaker, which is correct behavior.

---

## Summary of spec corrections

| Item | Spec said | Correct approach |
|------|-----------|-----------------|
| Langfuse in `LLMNEREnricher` | `get_langfuse_handler()` | `get_langfuse_client()`, manual generation span |
| `LLMNEREnricher` constructor | `llm` + `instructor_client` | `instructor_client: InstructorClient` only |
| `ConfidenceConfig` type | Python `@dataclass` | Pydantic `BaseModel` with `base_scores: dict[str, float]` |
| Key lookup in `calculate_confidence` | `config.base_scores.get(source, 0.50)` | `config.base_scores.get(source.value, 0.50)` |
| New `ExtractionConfig` fields | No defaults mentioned | All new fields have defaults matching `app.yaml` values |
