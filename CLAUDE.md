# CLAUDE.md

**Rule: Keep this file under 150 lines. Move detailed standards to `.claude/rules/` files and reference them here.**

## Project Context

Kebi is the AI engine for the Kebi product — an AI-native place decision engine. Users share places over time, the system builds a taste model, and returns one confident recommendation from natural language intent. This repo is pure Python: place extraction, embeddings, ranking, taste modeling, agent orchestration, and evaluations. The product repo calls this repo over HTTP only. Stack: Python 3.11, Poetry, FastAPI, LangGraph, LangChain, Pydantic, Instructor, pgvector, Redis, Langfuse. Models: claude-sonnet-4-6/Anthropic (orchestration), GPT-4o-mini/OpenAI (taste regen, vision), voyage-4-lite/Voyage AI (embeddings), whisper-large-v3-turbo/Groq (transcription). SDKs: OpenAI SDK, Anthropic SDK, Groq SDK, Voyage AI SDK. Deployed on Railway.

## Key Directories

- `src/kebi/` — main package (src layout)
  - `api/` — FastAPI routes and request/response schemas
  - `core/` — domain modules: extraction/, memory/, ranking/, taste/, agent/ (place extraction, memory and retrieval, ranking, taste modeling, agent orchestration)
  - `providers/` — LLM/embedding provider abstraction (config-driven via YAML)
  - `eval/` — evaluation harnesses and datasets
- `tests/` — pytest tests mirroring src structure
- `config/` — YAML configuration (`app.yaml` for all non-secret settings: app metadata, model roles, extraction config)
- `scripts/` — utility scripts
- `docs/` — operational docs: architecture, API contract, decisions log

See @.claude/rules/architecture.md for repo boundaries and coding constraints.

## Common Commands

```bash
poetry install                        # install dependencies
poetry run uvicorn kebi.api.main:app --reload      # dev server
poetry run pytest                     # run all tests
poetry run pytest tests/path/test_file.py::test_name    # single test
poetry run pytest -x                  # stop on first failure
poetry run ruff check src/ tests/     # lint
poetry run ruff format src/ tests/    # format
poetry run mypy src/                  # type check
docker compose up -d                  # start services (PostgreSQL, Redis) in detached mode
docker compose up -d --build          # start services and rebuild images
docker compose down                   # stop services
docker compose down -v                # stop services and remove volumes
```

## Standards

- **Naming**: snake_case everywhere. Pydantic models are PascalCase. Files match module name.
- **Types**: All function signatures typed. Pydantic models for all LLM input/output schemas. `mypy --strict` is the target.
- **Secrets management** (ADR-051): `.env` in the project root (gitignored symlink → `kebi-config/secrets/ai.env.local`). Copy `.env.example`, fill in your secrets — never committed. CI/CD injects secrets as environment variables at deploy time.
- **Provider abstraction**: `config/app.yaml` under `models:` maps logical roles (orchestrator, extractor, embedder, taste_regen, vision_frames, transcriber) to provider + model + params. Code never hardcodes model names — always reads from config.
- **API versioning**: All FastAPI routes live under `/v1/` prefix to match the product repo convention.
- **Repo boundary**: This repo owns all AI/ML logic. No UI, no auth, no CRUD. The product repo calls this repo via `POST /v1/chat` (unified conversational entry — ADR-052) and `GET /v1/health` (see `docs/api-contract.md`). Never import from or depend on the product repo.
- **Pydantic everywhere**: Request/response schemas, LLM output parsing, internal data transfer — all Pydantic. No raw dicts crossing function boundaries.
- **LangGraph for orchestration**: Agent workflows use LangGraph graphs, not raw chains.
- **Code quality** — single responsibility, `Depends()` only (no construction inside functions), abstract base class over if/match on provider, repository pattern for all DB access, no duplication (extract to `app/utils/`), new behavior = new class not an edit. Violations must be fixed before presenting code.

See @.claude/rules/architecture.md for full constraints.

## Workflow

See `.claude/workflows.md` for the complete 5-step token-efficient workflow (ADR-028):

1. **Clarify** — If ambiguous (3+ unknowns), ask 5 questions. Record answers in chat.
2. **Plan** — If 3+ files or crosses repo boundary, create `docs/plans/YYYY-MM-DD-<feature>.md` with phases and checklist.
3. **Implement** — Follow plan checklist, write code, commit per `.claude/rules/git.md`.
4. **Verify** — Run verify commands from plan (`pytest`, `ruff check`, `mypy`), all must pass.
5. **Complete** — Mark task done. Update task status only.

**IMPORTANT: Read `docs/decisions.md` FIRST — before planning, before implementing, before any architectural discussion.** Every ADR is a binding constraint. If your approach contradicts a decision, stop and flag it. This is the first thing you do, not a later verification step.

**IMPORTANT: After each completed task or important architectural change, write a new ADR in `docs/decisions.md`.** Append a new numbered ADR (next free ADR-NNN) capturing the decision, context, and consequences. No task is "done" until its ADR is recorded.

**Constitution Check:** Verify plan aligns with `docs/decisions.md` (see `.claude/constitution.md`).

**Agent Skills Integration:** If agent skills are installed for this repo, they auto-activate based on code domain and workflow stage, not user prompts. Python/FastAPI-focused skills (if any) guide implementation of intent parsing, embeddings, ranking, and agent orchestration. All skill guidance defers to project standards — if a skill recommendation conflicts with `CLAUDE.md`, `architecture.md`, or ADRs, project standards take precedence. Skills are helpers for exploration and implementation, never overrides for project constraints. In particular: provider abstraction patterns, Pydantic schemas, type safety (`mypy --strict`), and LangGraph workflows are binding — no skill bypasses these.

**Model assignments and token costs:** See `.claude/workflows.md` (source of truth).

See @.claude/rules/git.md for branch naming, commit format, and merge flow.

## Notes

- **Task-driven workflow.** Each task arrives scoped — execute it. No phase gates.
- **Git comment char is `;`** not `#`. Configured in this repo's git config. Commit messages and interactive rebase use `;` for comments.
- **Secrets in `.env`**: Root `.env` (gitignored symlink). Non-secret config (app metadata, models, extraction weights) lives in `config/app.yaml` (committed). If a command fails with missing API key, check `kebi-config/secrets/ai.env.local`.
- **Database write split**: Shared PostgreSQL instance on Railway. This repo writes AI data (places, embeddings, taste_model, recommendations, user_memories, interaction_log) and owns their migrations via Alembic. NestJS writes product data (users, user_settings) via TypeORM with `synchronize: true`. Never cross ownership boundaries.
- **Redis caching**: LLM responses are cached in Redis. When changing prompt templates or model config, consider cache invalidation.
- **Langfuse tracing**: All LLM calls should be traced via Langfuse. Missing traces usually means the Langfuse callback handler wasn't attached.
- **API testing**: Bruno collection at `kebi-config/bruno/`. New endpoints should have a corresponding `.bru` request file added there.

## Recent Changes
- movement-scope (ADR-084): every agent turn now resolves a **search scope** alongside the working location (ADR-083) — folded into the existing `resolve_location` node, no new node/LLM. `POST /v1/chat` gains optional `movement_profile` (`{default_mode, available_modes, reach}`; modes `walking|cycling|motorbike|driving|transit|rideshare`) — owned by the product repo's `user_settings`, sent each turn like `location`, never stored. New: `core/agent/location.py` gains `MovementMode`/`Reach`/`ScopeTier`/`ScopeShape`/`DensityClass` literals, `CorridorTarget`, `density_class()`, `resolve_radius()`; `WorkingLocation` gains `effective_mode`/`scope_tier`/`scope_shape`/`search_radius_m`/`corridor`/`density`/`bbox`; `LocationResolution` gains `scope_tier`/`scope_shape`/`effective_mode`/`corridor_destination`. `resolve_radius = radius_tiers[shift(tier,reach)] × mode_multiplier[mode] × density_factor[density]` — all config (`movement:` block in `app.yaml`, `MovementConfig`), LLM never emits the number. Density is read from the geocoder place type (`density_class`), not a static table. `NominatimGeocodingClient` now returns `GeocodeResult` (coords + `place_type` + `bbox`) from `search`/`forward`/`reverse`; added free-text `search()`. `AgentState` gains `movement_profile` (plain overwrite, **no reducer** — re-supplied per turn, never carried). `agent.txt` gains `{movement_context}` slot; `location_resolver.txt` gains a mobility-profile slot + scope/mode classification rules. `build_turn_payload`/`ChatService`/`chat_stream` thread `movement_profile`. Corridor ("on my way…") destinations are eagerly geocoded; an ungeocodable/implicit anchor asks the user (no silent area fallback). No `movement_profile` on the request → neutral config `fallback`. Scope is recorded for a future place-search consumer (ADR-075 removed recall) — today it only shapes the agent's prose. Pre-existing `places_v2` `hybrid_search_repo` REGCONFIG mypy/pytest failures remain unrelated/out of scope.
- drop-chips-tier-signals (ADR-076): the chip artifact, the chip lifecycle, the derived signal tier, and the `onboarding_confirm`/`onboarding_dismiss`/`chip_confirm` interaction signals are all removed. `GET /v1/user/context` is **deleted entirely** (saved_count included); `POST /v1/signal` narrows to recommendation accept/reject only (no longer a discriminated union); `POST /v1/chat` drops the `signal_tier` field. Deleted files: `core/taste/tier.py`, `core/taste/chip_merge.py`, `api/schemas/user_context.py`, `tests/core/taste/{test_tier,test_chip_merge}.py`, `tests/api/routes/test_user.py`, `tests/core/signal/test_service.py`. `core/taste/schemas.py` loses `SignalTier`/`ChipStatus`/`Chip`/`ChipView`/`TasteContext`/`UserContext`; `TasteArtifacts`/`TasteProfile` lose `chips`. `TasteModelService` drops `get_taste_context`/`run_regen_now` and all chip/merge logic in `_run_regen`; `regen.py` is summary-only (no `existing_chips`, no `_prune_path`). `events.py` drops `OnboardingSignal`/`ChipConfirmed`; `handlers.py` drops the onboarding branch + `on_chip_confirmed`; `deps.py` drops the `onboarding_signal`/`chip_confirmed` registrations and `SignalService` no longer takes `taste_service`. `db/models.py`: `InteractionType` down to `save/accepted/rejected`, `taste_model.chips` column removed; `taste_model_repository` drops `merge_chip_statuses` + the `chips` arg on `upsert_regen`. Migration `d4e5f6a7b8c9` purges the three interaction types, rebuilds the `interactiontype` enum, drops the `chips` column (schema-reversible, not data-reversible). `config/app.yaml` + `TasteModelConfig` drop `chip_threshold`/`chip_max_count`/`chip_selection_stages`; `config/prompts/taste_regen.txt` rewritten summary-only. `warming_blend` kept (dormant consult knob, out of scope). Supersedes ADR-061; obsoletes the `/v1/user/context` + chips parts of ADR-060/058. Recommendation-signal path + dormant `recommendations` table (ADR-075) untouched. Pre-existing `places_v2` REGCONFIG mypy/pytest failures remain unrelated/out of scope.
- drop-recall-consult (ADR-075): the recall and consult services AND both agent tools are deleted. The LangGraph agent is now a **zero-tool conversational Q&A surface** — `/v1/chat` + `/v1/chat/stream` stay (agent answers from knowledge + taste/memory context, redirects place save/recall/recommend to product surfaces). Deleted: `core/recall/`, `core/consult/`, `agent/tools/{recall_tool,consult_tool,_emit,_timeout}.py`, `api/schemas/{recall,consult}.py`, `db/repositories/recall_repository.py`, `core/places/filters.py`, and their tests. `build_tools()` takes no args → `[]`. `ChatService.__init__` drops `consult_service`/`recall_service`; `get_agent_graph`/`get_chat_service` rewired. `ReasoningStep` loses `tool_name`; `source` ∈ {`agent`,`fallback`} (no `tool`); `AgentState.last_recall_results` removed. `ChatResponseType` narrowed to `["agent","error"]`; `Location` moved into `schemas/chat.py`. `RecallConfig` removed; `ConsultConfig` **kept** (still read by deferred-scope legacy `core/places` discover/validate path — documented, not pruned). `recommendations` table + repo + accept/reject signal path + handlers kept **dormant** (still read for signal validation, never written). Supersedes ADR-058; obsoletes recall/consult parts of ADR-052/060/062/064/065/073. Product-repo contract: `/v1/chat` never returns `consult`/`recall` types, stream emits no tool-result frames. Pre-existing `places_v2` mypy/pytest failures (`REGCONFIG` SQLAlchemy dialect) are unrelated and out of scope. save→recall→consult loop has no server-side recall/recommendation until the replacement lands (accepted temporary gap).
- extraction-result-cache (ADR-074): `POST /v1/extract` now consults a Redis cache keyed by canonical URL (`extract:v1:{sha256}`, 30-day TTL) before running the pipeline. Cache hit → skip yt-dlp/Whisper/vision/picker/Google, call `save_places` directly with cached `PlaceCore`s, fire `PlaceSaved`, return in ~50ms. Cache miss → pipeline runs as before, completed responses are written to the cache. `parse_input` now applies `canonicalize_url` so `ParsedInput.url` is the canonical form (TikTok `/photo/` → `/video/` rewrite + query/fragment strip on tiktok/instagram/youtube hosts and their `www.`/`m.`/`vm.` variants); the former `normalize_url` is folded into `canonicalize_url`. `ExtractPlaceItem.place` narrowed from `PlaceObject` to `PlaceCore` — extraction never populated live fields (rating, hours, popularity); those come from `PlacesService.enrich_batch` at recall/consult time. FK-violation fallback evicts stale cache entries and re-runs the pipeline. Out of scope (follow-up): shortlink expansion (`vm.tiktok.com`, `maps.app.goo.gl`, `youtu.be`), per-platform identity normalization (TikTok `post_id`-only), universal tracking-param strip for unknown hosts, back-fill of existing `user_places.source_url` rows.
- drop-save-tool (ADR-073): the agent's save tool is removed; extraction is HTTP-only via `POST /v1/extract`. `/v1/chat` is conversation-only (recall + consult) — no `user_places` writes. `ChatService.__init__` no longer takes `extraction_service` (it was held but never used); `get_agent_graph` drops the same dep; `build_tools` is now `(recall, consult)`. `_TOOL_DECISION_FALLBACKS`, `ReasoningStep.tool_name`, and `ToolName` Literal narrowed to `["recall", "consult"]`. `ChatResponseType` Literal drops `"extract-place"` and `"clarification"` — both depended on the save tool. The `GraphInterrupt → clarification` handler in `_run_agent` is deleted (no current producer of GraphInterrupt). Files deleted: `src/kebi/core/agent/tools/save_tool.py`, `tests/core/agent/tools/test_save_tool.py`, `tests/api/routes/test_chat_interrupt.py`. Agent prompt narrows to two tools and gains a one-line redirect for URL submissions. `POST /v1/extract` is promoted from "debug/internal" to canonical product entry point; `GET /v1/extraction/{request_id}` retained as reserved infrastructure for a future async variant. ExtractionService, ExtractionPipeline, enrichers, and `get_extraction_service` unchanged.
- 030-places-v2-migration: extraction flow cuts over to `core/places_v2/` only (ADR-070, ADR-071). `core/extraction/searcher.py` deleted — `places_v2.PlacesSearchService` is the single source of truth for place lookups (DB-first, cache overlay, provider fallback, upsert). `core/extraction/persistence.py` and `ExtractionPersistenceService` deleted (ADR-071 supersedes ADR-057) — confidence partition at save time deprecated; extraction inlines `PlaceUpsertService.upsert_and_embed` then `UserPlacesService.save_places` (with `approved=False`, user curates later). Duplicates caught via `DuplicateUserPlaceError` (carries `conflicts: list[str]`) — filter and retry, no UserPlacesRepo injection. `core/extraction/types.py` drops every legacy place re-export; `ValidatedCandidate` reshaped to v2 vocabulary (`PlaceCategory` list, `PlaceTag` list, namespaced `provider_id`, v2 `PlaceSource`); `SearchMatch` deleted. LLMPlacePicker output schema rewritten to v2 vocab. New extraction-side files: `geo_filter.py` (drops geographic-feature matches from `PlacesSearchService` results) and `candidate_mapper.py` (`ValidatedCandidate` → v2 `PlaceCore`, plus `reconcile_picks` + `AttributedSearchResult` moved from the deleted searcher). `ExtractPlaceItem.status` field dropped entirely — response is a flat list of places now associated with the user; the save_tool's `needs_review` NodeInterrupt branch is gone with the field. `core/places_v2/` is frozen — every bridging concern lives extraction-side. Read paths (recall, consult, agent recall, user-places API, taste) intentionally NOT migrated; save→recall loop temporarily broken until follow-up feature. Three externally observable JSON changes: `place_type: string` → `categories: string[]`, `attributes: {...}` → `tags: [{type,value,source}]`, `status` field removed from each item — see specs/030-places-v2-migration/contracts/http-response-parity.md.
- extraction-cascade-refactor (2026-04-24): extract-place pipeline restructured around `EnrichmentLevel` (text/signal producers) + a single pipeline-owned NER finalizer (`LLMNEREnricher`). Replaces the prior `EnrichmentPipeline` + `background_enrichers` shape — both phases were doing the same enrich → dedup → validate dance with duplicated bookkeeping. Subtitle/Whisper become pure transcript producers (NER stripped out); Vision unchanged. New per-request `limit` parameter on `ExtractionService.run` (default `DEFAULT_MAX_CANDIDATES = 25` in `service.py`, no config knob — pipeline takes a concrete `limit: int`) hard-drops requests over the cap pre-validation via `TooManyCandidatesError`, protecting Google/DB/Voyage quota from noisy inputs. `ExtractionContext.source` auto-derived from URL via `core/extraction/url_source.py`; new `SourceFilteredEnricher` base class lets enrichers declare `allowed_sources` and short-circuit unsupported URLs (TikTok oEmbed → tiktok only; yt-dlp → tiktok/instagram/youtube).
- 024-agent-tool-migration: LangGraph agent (Claude Sonnet) replaces intent-router dispatch (ADR-062, ADR-065). Three tools — recall, save, consult. ConsultService signature changed to take agent-parsed args. ExtractPlaceResponse schema upgraded to two-level status (ADR-063). Reasoning traces via service-emit / wrapper-wrap pattern (ADR-064). SSE streaming at `POST /v1/chat/stream`. NodeInterrupt on `needs_review` saves (M8). Per-tool timeouts + failure-budget guard (M9). Deleted: IntentParser, classify_intent, ChatAssistantService, and the intent_router / intent_parser / chat_assistant / evaluator model roles (evaluator was reserved but never wired). `GET /v1/extraction/{request_id}` polling route retained for background extractions.


