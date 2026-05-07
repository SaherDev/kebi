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
- extraction-cascade-refactor (2026-04-24): extract-place pipeline restructured around `EnrichmentLevel` (text/signal producers) + a single pipeline-owned NER finalizer (`LLMNEREnricher`). Replaces the prior `EnrichmentPipeline` + `background_enrichers` shape — both phases were doing the same enrich → dedup → validate dance with duplicated bookkeeping. Subtitle/Whisper become pure transcript producers (NER stripped out); Vision unchanged. New per-request `limit` parameter on `ExtractionService.run` (default `DEFAULT_MAX_CANDIDATES = 25` in `service.py`, no config knob — pipeline takes a concrete `limit: int`) hard-drops requests over the cap pre-validation via `TooManyCandidatesError`, protecting Google/DB/Voyage quota from noisy inputs. `ExtractionContext.source` auto-derived from URL via `core/extraction/url_source.py`; new `SourceFilteredEnricher` base class lets enrichers declare `allowed_sources` and short-circuit unsupported URLs (TikTok oEmbed → tiktok only; yt-dlp → tiktok/instagram/youtube).
- 024-agent-tool-migration: LangGraph agent (Claude Sonnet) replaces intent-router dispatch (ADR-062, ADR-065). Three tools — recall, save, consult. ConsultService signature changed to take agent-parsed args. ExtractPlaceResponse schema upgraded to two-level status (ADR-063). Reasoning traces via service-emit / wrapper-wrap pattern (ADR-064). SSE streaming at `POST /v1/chat/stream`. NodeInterrupt on `needs_review` saves (M8). Per-tool timeouts + failure-budget guard (M9). Deleted: IntentParser, classify_intent, ChatAssistantService, and the intent_router / intent_parser / chat_assistant / evaluator model roles (evaluator was reserved but never wired). `GET /v1/extraction/{request_id}` polling route retained for background extractions.
- 023-onboarding-signal-tier: Signal tier (cold/warming/chip_selection/active) derived from config-driven `chip_selection_stages` (ADR-061). `GET /v1/user/context` returns `signal_tier` + chips with `status`/`selection_round`. Product repo gates tier routing — it reads `/v1/user/context` and forwards `signal_tier` on `/v1/chat` + `/v1/consult` requests; `ConsultResponse` is NOT extended with an envelope. New `chip_confirm` variant on `POST /v1/signal` writes a CHIP_CONFIRM interaction row with metadata, merges chip statuses (confirmed immutable; rejected may resurface when signal grows), and dispatches `ChipConfirmed` which forces an immediate taste-profile rewrite. Warming tier applies a config-driven 80/20 discovered/saved candidate-count blend.


## Active Technologies
- `PlaceFilters` / `RecallFilters` / `ConsultFilters` family in `core/places/filters.py` + `core/recall/types.py` (Pydantic extensions of a shared base mirroring `PlaceObject` per ADR-056)
- `EmitFn` primitive callback pattern in `core/emit.py`; recall/consult/extraction services gain optional `emit` parameter; `ConsultResponse.reasoning_steps` removed
- `core/agent/tools/` module (LangGraph 0.3 tools via `@tool` + `Annotated[..., InjectedState]`; `saved_places` threaded tool→tool via `state["last_recall_results"]`; `_emit.py` helpers fan out to `langgraph.config.get_stream_writer()`; `_timeout.py` wraps each tool in `asyncio.wait_for`); SSE at `POST /v1/chat/stream`; compiled graph warmed eagerly in `api/main.py` lifespan
- Extraction cascade: `EnrichmentLevel` dataclass in `core/extraction/enrichment_level.py` (producers + dedup + summary_fn + requires_url) wired into `ExtractionPipeline.levels: list[EnrichmentLevel]` plus a shared `finalizer: Enricher` (production: `LLMNEREnricher`); per-level `summary_fn` builds the SSE step text. `SourceFilteredEnricher` base in `core/extraction/source_filtered_enricher.py` (declares `allowed_sources`, runs URL/source guard before `_run`). `TooManyCandidatesError` raised by `_enforce_candidate_limit` when `len(candidates) > max_candidates` — service catches it and returns `failed`.

