# Research: Place Extraction Endpoint (Phase 2)

**Branch**: `002-extract-place` | **Date**: 2026-03-24

No NEEDS CLARIFICATION items were found — the stack is fully defined by existing ADRs and the current codebase. This file documents the key design decisions reached through reading the codebase and reference materials.

---

## 1. Instructor integration with existing LLM provider abstraction

**Decision**: Add `get_instructor_client(role: str) -> InstructorClient` to `src/kebi/providers/llm.py`. `InstructorClient` is a thin wrapper that holds an instructor-patched async OpenAI client and the config-resolved model name. The wrapper exposes a single typed `extract()` method that callers use instead of calling Instructor directly. This keeps the config-driven model selection (ADR-020) intact — no model name is hardcoded in extractor classes.

**Rationale**: Instructor's `from_openai()` wraps an `AsyncOpenAI` client. The model name must be passed at call time, not at client construction. A wrapper class bundles both so extractor classes receive a single injectable dependency and never touch config directly.

**Alternatives considered**:
- `instructor.from_provider("openai/gpt-4o-mini")` inline in extractors — rejected, hardcodes model name, violates ADR-020.
- Separate `providers/instructor.py` file — rejected, unnecessary split when `llm.py` already owns provider concerns.

---

## 2. TikTok oEmbed HTTP call

**Decision**: Use `httpx.AsyncClient` with a 3-second timeout for the oEmbed call (`GET https://www.tiktok.com/oembed?url={url}`). The `TikTokExtractor` creates the client at extract time (not at construction) to keep the class stateless. On timeout or non-200 response, the extractor raises a domain exception that the service maps to a 500 error.

**Rationale**: `httpx` is already in the project (dev deps) and is the idiomatic async HTTP client for FastAPI. Moving it to production deps is the only change required. The 3-second timeout was set in the clarification session to fit within the 10-second total response budget.

**Alternatives considered**:
- `aiohttp` — already have httpx, no reason to add another HTTP client.
- `requests` (sync) — incompatible with async FastAPI handlers.

**New production dependency**: `httpx = "^0.28"` (promote from dev group to main).

---

## 3. Instructor exception handling

**Decision**: Handle all three Instructor exception types in each extractor's `extract()` method:
- `IncompleteOutputException` → log warning, return `None` (caller treats as extraction failure)
- `InstructorRetryException` → log error with attempt count, return `None`
- `ValidationError` (pydantic) → log error, re-raise (hard schema violation, not retryable)

The service layer catches `None` from `extract()` and raises `ExtractionFailedNoMatchError`.

**Rationale**: These three types cover all Instructor failure modes documented in the Phase 2 learning material. Returning `None` on soft failures lets the service decide the response strategy without the extractor knowing about HTTP error codes.

---

## 4. Google Places Text Search API

**Decision**: `GooglePlacesClient.validate_place()` calls the Places API `findplacefromtext` endpoint with `inputtype=textquery` and `fields=name,formatted_address,place_id`. Match quality is computed by comparing the extracted name to the returned name using `difflib.SequenceMatcher`:
- Ratio ≥ 0.95 → `EXACT`
- Ratio ≥ 0.80 → `FUZZY`
- Place found but name ratio < 0.80 → `CATEGORY_ONLY`
- No result → `NONE`

API key is read from `load_yaml_config(".local.yaml")`, never directly from `os.environ`. The application code has no knowledge of environment variables—all fallback handling is delegated to `config.py`, which loads from `.local.yaml` first, then falls back to environment variables if the file is missing. This separation ensures app code only depends on loaded config, not runtime environment details.

**Rationale**: Text Search is the correct endpoint for name-based lookup. The `findplacefromtext` endpoint accepts a free-text query and returns the best match. String similarity provides a deterministic match quality without requiring an additional LLM call. Reading only from config preserves the abstraction boundary: app code never knows about environment variables, only about configuration objects.

**Alternatives considered**:
- Nearby Search — requires coordinates; not always available from caption text.
- Place ID lookup — requires a known place ID, not applicable at extraction time.
- Direct env var access in GooglePlacesClient — rejected, violates clean architecture; app should not know about environment variable names or fallback logic.

---

## 5. DB model additions (Alembic migration required)

**Decision**: Add three nullable columns to the `places` table:

| Column | Type | Notes |
|--------|------|-------|
| `google_place_id` | `VARCHAR, nullable` | External ID from Google Places — never PK (clarification Q2) |
| `confidence` | `FLOAT, nullable` | Score at time of save (only populated for saved records, ≥ 0.70) |
| `source` | `VARCHAR, nullable` | Extraction source: `CAPTION`, `PLAIN_TEXT`, `SPEECH` (Phase 3), `OCR` (Phase 3) |

One new Alembic migration: `add_extraction_metadata_to_places`.

**Rationale**: UUID `id` is already the PK. `google_place_id` is stored as a lookup aid and future deduplication key — not a PK — so swapping Google Places for another provider (e.g. Foursquare) only requires adding a `foursquare_place_id` column, not a PK migration.

---

## 6. Confidence weights in config

**Decision**: All weights live in `config/.local.yaml` under `extraction.confidence_weights`:

```yaml
extraction:
  confidence_weights:
    base_scores:
      CAPTION: 0.70
      PLAIN_TEXT: 0.70
      SPEECH: 0.60      # Phase 3 — defined now, no implementation yet
      OCR: 0.55         # Phase 3 — defined now, no implementation yet
    places_modifiers:
      EXACT: 0.20
      FUZZY: 0.15
      CATEGORY_ONLY: 0.10
      NONE_CAP: 0.30    # max score when no Places match
    multi_source_bonus: 0.10
    max_score: 0.95
  thresholds:
    store_silently: 0.70
    require_confirmation: 0.30
```

`compute_confidence()` reads these at call time via `load_yaml_config(".local.yaml")`.

---

## 7. New production dependency

**Decision**: Add `instructor` to pyproject.toml main dependencies.

```toml
instructor = "^1.0"
```

Also promote `httpx` from dev to main:
```toml
httpx = "^0.28"
```

---

## 8. Claude Code harness config separation from application config

**Decision**: `.claude/settings.local.json` is a Claude Code tool harness configuration file, not an application configuration file. The application has zero knowledge of this file and never reads it. It exists only to configure Claude Code's behavior during development (permissions, hooks, environment setup). This file is created by the harness at deployment time (e.g., on Railway when environments are available) to configure Claude Code permissions for that environment.

**Boundary**:
- Application config: `config/*.yaml` files only (read by `load_yaml_config()`)
- Claude Code harness config: `.claude/settings.local.json`, `.claude/rules/`, `.claude/workflows.md` (Claude Code tool only, never read by app)
- Environment variables: Handled by `config.py` as a fallback when `.local.yaml` is missing; app never reads environment directly

**Rationale**: This separation ensures the application code and the development harness remain decoupled. The app doesn't leak implementation details about how the harness configures permissions or what environment it's running in. If `.claude/settings.local.json` is added to `.gitignore`, it won't affect the app — the app continues to work the same way.

---

## 9. Constitution inconsistency (non-blocking)

ADR-030, CLAUDE.md, and the existing Alembic migration files are the authoritative source: Alembic owns AI tables in this repo; TypeORM in the product repo manages users and user_settings. Proceed per ADR-030.
