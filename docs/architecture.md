# System Architecture — Kebi

## Overview

This repo (kebi) is the AI engine of Kebi. It owns all AI logic: intent parsing, place extraction, embedding generation, vector retrieval, external discovery, ranking, taste model, and agent orchestration. It runs as a standalone FastAPI service that the product repo calls over HTTP.

```
┌──────────────────────────────────┐
│   product repo                   │
│   NestJS backend                 │
│   Sends auth-verified requests   │
└───────────────┬──────────────────┘
                │ HTTP (JSON)
                │
                │  POST /v1/chat
                │  GET  /v1/health
                ▼
┌──────────────────────────────────────────────────────────┐
│                kebi (this repo)                      │
│                                                           │
│  FastAPI HTTP layer                                       │
│  LangGraph agent orchestration (tool dispatch)            │
│  LangChain chains and document loaders                    │
│  Pydantic request/response schemas                        │
│  Provider abstraction (LLM + embedding model switching)   │
└──┬───────────────┬──────────────┬──────────────┬─────────┘
   │               │              │              │
   │ SQL           │ HTTP         │ HTTPS        │ TCP
   │ (read-write)  │              │              │
   ▼               ▼              ▼              ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ PostgreSQL   │ │ Google       │ │ Groq / OpenAI│ │ Redis        │
│ + pgvector   │ │ Places API   │ │ / Anthropic  │ │ (cache)      │
│              │ │              │ │ / Voyage AI  │ │              │
│ Writes:      │ │ Validate     │ │              │ │ LLM response │
│ - places     │ │ places       │ │ LLM inference│ │ caching      │
│ - embeddings │ │ Discover     │ │ Embeddings   │ │ Extraction   │
│ - taste_model│ │ nearby       │ │ Transcription│ │ status       │
│ - consult_   │ │ Geocode      │ │              │ │ Agent state  │
│   logs       │ │              │ │              │ │              │
│ - user_      │ │              │ │              │ │              │
│   memories   │ │              │ │              │ │              │
│ - interaction│ │              │ │              │ │              │
│   _log       │ │              │ │              │ │              │
│ Reads:       │ │              │ │              │ │              │
│ - all tables │ │              │ │              │ │              │
└──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘
```

## What This Repo Owns

- Natural language understanding (intent parsing)
- Place extraction from URLs, free text, and screenshots
- Embedding generation
- Writing extracted places and embeddings to PostgreSQL
- Vector similarity search (pgvector queries)
- External place discovery via Google Places API
- Ranking and scoring algorithms (deterministic, tunable)
- Taste model construction and reading
- Agent orchestration via LangGraph
- LLM provider abstraction (model switching via config)
- LLM response caching via Redis
- Evaluation pipeline (retrieval accuracy, agent task completion, token cost, latency)

## What This Repo Does NOT Do

- Serve UI. No HTML, no templates, no static files.
- Manage auth. The product repo validates users before calling.
- Own database migrations for product tables. Alembic in this repo manages migrations for places, embeddings, and taste_model only.
- Write user records, settings, or recommendation history. Those are product data owned by NestJS.

## Data Flow: Extract a Place

extract-place is a level-driven deterministic workflow, not an agent. No LangGraph. No tool selection. No reasoning loop. The full pipeline always runs as a background task — the HTTP response returns immediately with `status="pending"` and a `request_id`. The caller polls `GET /v1/extraction/{request_id}` for the final result.

The pipeline is built from `EnrichmentLevel`s (text/signal producers grouped by stage) plus a single shared **finalizer** (today: `LLMNEREnricher`) that runs after every executed level. Each level has a `name`, an `enrichers` list, an optional `requires_url` flag, and a `summary_fn`. Adding a new stage is: declare a new `EnrichmentLevel`, append it to the levels list. The pipeline loop is identical for every level.

```
Raw input (URL, plain text, or mixed)
    │
    ▼
POST /v1/chat
    │  Agent selects save tool → ExtractionService.run(raw_input, user_id, limit?)
    │  Returns immediately: { status: "pending", request_id }
    │  asyncio.create_task fires the full pipeline in the background
    │
    └── Background task: ExtractionPipeline.run()
        │  context = ExtractionContext(url, user_id, supplementary_text)
        │  context.source auto-derived from url via source_from_url()
        │  effective_limit = limit ?? DEFAULT_MAX_CANDIDATES  (default 25)
        │
        ├── For each EnrichmentLevel in [inline, deep]:
        │   ├── If level.requires_url and url is None → skip
        │   ├── Run producers in order; dedup_candidates(context)
        │   │   inline: Parallel(TikTokOEmbed, YtDlp) — both gated to context.source
        │   │   deep:   Subtitle → Whisper → Vision (each populates transcript or
        │   │           appends candidates directly)
        │   ├── Run pipeline finalizer (LLMNEREnricher): one consolidated NER call
        │   │   over caption + transcript + supplementary_text; dedup again
        │   ├── Emit save.{name} reasoning step
        │   ├── Cap-check: if len(candidates) > effective_limit
        │   │   → emit save.cap_exceeded, raise TooManyCandidatesError, drop request
        │   ├── validator.validate(candidates) — Google Places parallel fan-out
        │   └── If results non-empty: emit save.validate, dedup_validated_by_provider_id,
        │       short-circuit return  ← early exit, deeper levels never run
        │
        ├── If loop exits with no validated results: emit save.validate
        │   "Could not confirm any places", return []
        │
        └── ExtractionService persists each surviving outcome → emit save.persist
            → write ExtractPlaceResponse to extraction:v2:{request_id} in Redis

GET /v1/extraction/{request_id}
    Reads Redis → returns ExtractPlaceResponse (same shape as chat data payload)
    404 while still running or after TTL (1 hour) expires
```

The pipeline runs the full cascade deterministically. No mid-pipeline callbacks to NestJS.

**Cap semantics (hard drop, not truncate):** when an enrichment level produces more candidates than `effective_limit`, the request is dropped entirely — no Google validation, no DB writes, no embeddings. The service returns `status="failed"` with a `save.cap_exceeded` reasoning step so the caller can show the user what happened. This protects Google quota, DB, and Voyage from a noisy single input (e.g. a 200-place text dump).

**Source-filtered enrichers:** `ExtractionContext.source` is auto-derived from the URL via `source_from_url()` (in `core/extraction/url_source.py`). Enrichers that subclass `SourceFilteredEnricher` declare `allowed_sources` in `__init__` and short-circuit before doing any work when `context.source` is unsupported. Today: `TikTokOEmbedEnricher` runs only for `PlaceSource.tiktok`; `YtDlpMetadataEnricher` for `{tiktok, instagram, youtube}`. Prevents guaranteed-failure URLs from tripping the circuit breaker on noise.

## Data Flow: Consult (Recommend a Place)

consult is a sequential 6-step pipeline implemented in `ConsultService` as a plain Python class (ADR-050: LangGraph parallelization deferred). Each step passes only the data the next step needs, not the full payload from prior steps. ConsultService persists a `consult_logs` record directly after building the response (ADR-053) — NestJS does not store recommendation history.

```
Natural language query (e.g., "cheap dinner nearby")
    │
    ▼
POST /v1/chat
    │  Receives: user_id, message, optional location
    │  Agent selects consult tool → ConsultService.consult() called with agent-parsed args
    │
    ▼
ConsultService.consult()
    │
    ├── Step 1: Parse intent
    │   Agent (Claude Sonnet 4.6) parses intent and passes structured args directly;
    │   ConsultService receives pre-parsed cuisine, occasion, price, radius, constraints
    │
    ├── Step 2: Retrieve saved places
    │   Hybrid search (pgvector + FTS + RRF) via RecallService
    │   Post-filter by price_range and radius if location available
    │
    ├── Step 3: Discover external candidates (parallel)
    │   ├── Keyword search: Google Places nearby search with query + radius → source="discovered"
    │   └── Suggestion validation: agent-supplied place names validated via
    │       validate_places() — EXACT/FUZZY matches only, anchored to same
    │       geocoded location_bias → source="suggested"
    │   Skipped if no location context
    │
    ├── Step 4: Deduplicate candidates (by provider_id, then place_id)
    │   Order: saved → discovered → suggested
    │   ConsultResult.source ∈ {"saved", "discovered", "suggested"}
    │
    ├── Step 5: Conditional validation of saved candidates
    │   Validates against live signals only when opennow filter is set
    │
    ├── Step 6: Rank all candidates
    │   Deterministic scoring: taste fit, distance, price, popularity
    │
    └── Step 7: Build response + persist consult log
        Return 1 primary + up to 2 alternatives with reasoning steps
        Persist ConsultLog record (ADR-053); write failures are logged, not raised
```

The pipeline runs fully within FastAPI. No mid-pipeline callbacks to NestJS.

## Data Flow: Recall (Retrieve Saved Places)

recall is a hybrid search workflow combining vector similarity (pgvector) and full-text search (PostgreSQL FTS) with Reciprocal Rank Fusion (RRF) merging. It retrieves user's saved places matching a natural language query.

```
Natural language query (e.g., "cosy ramen spot")
    │
    ▼
POST /v1/chat
    │  Agent selects recall tool → RecallService called with agent-parsed query
    │
    ├── Check cold start
    │   count_saved_places(user_id)
    │   If 0: return empty_state=true (no places to search)
    │
    ├── Embed query
    │   Try: query_vector = await embedder.embed(query, input_type="query")
    │   Catch RuntimeError: set query_vector=None (fallback to text-only)
    │
    ├── Hybrid search with RRF merge
    │   If query_vector is not None:
    │     - vector_results: pgvector cosine similarity (<=> operator)
    │       Top limit×candidate_multiplier candidates pre-fetched
    │     - text_results: PostgreSQL FTS (place_name + cuisine)
    │       Matches via plainto_tsquery + ts_rank scoring
    │     - combined: FULL OUTER JOIN with RRF score calculation
    │       RRF formula: 1/(k + rank) per method, summed across both
    │       k=60 (constant, prevents rank=0 dominance)
    │       min_rrf_score=0.01 (threshold filters low-relevance results)
    │   Else (embedding failed):
    │     - text_only_search: FTS fallback (no vector component)
    │
    ├── Derive match_reason (deterministic, no LLM)
    │   CASE statement in SQL:
    │   - matched_vector=true AND matched_text=true
    │     → "Matched by name, cuisine, and semantic similarity"
    │   - matched_vector=true only
    │     → "Matched by semantic similarity"
    │   - matched_text=true only
    │     → "Matched by name or cuisine"
    │
    └── Return to NestJS
        results: list of RecallResult (place_id, place_name, address, cuisine, etc., match_reason)
        total: count of results
        empty_state: true if user has zero saves, false otherwise
```

**Key properties:**
- Single CTE query: no N+1 queries; all ranking in database
- RRF merging: fairly combines semantic (vector) and keyword (FTS) relevance
- Candidate multiplier: prevents vector results from starving FTS results
- Min RRF score threshold: filters low-relevance results; configurable via `min_rrf_score` (default 0.01)
- Graceful fallback: text-only path exists; embedding failure does not crash
- Deterministic match_reason: reflects actual search behavior; no guessing

## Agent Orchestration (ADR-062, ADR-065)

All conversational traffic routes through the LangGraph agent (Claude Sonnet 4.6 via the `orchestrator` model role). The agent selects from three tools per turn — recall, save, consult — and returns a `ChatResponse(type="agent")`. The legacy intent-router / intent-parser / chat_assistant dispatch path was deleted (ADR-065).

### Graph Structure

```
POST /v1/chat  (or /v1/chat/stream)
    │
    ▼
ChatService
    │  build_turn_payload(message, user_id, taste_summary, memory_summary, location)
    │  graph.ainvoke(payload, config={"configurable": {"thread_id": user_id}})
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│                    LangGraph StateGraph                  │
│                                                          │
│   ┌──────────┐   tool_calls?  ┌──────────┐              │
│   │  agent   │ ─────────────► │  tools   │              │
│   │  (node)  │ ◄────────────  │  (node)  │              │
│   └────┬─────┘                └──────────┘              │
│        │ max_steps / max_errors exceeded?               │
│        ▼                                                 │
│   ┌──────────┐                                           │
│   │ fallback │ ──────────────────────────────► END       │
│   │  (node)  │                                           │
│   └──────────┘                                           │
│        │ no tool calls?                                  │
│        └─────────────────────────────────────► END       │
└─────────────────────────────────────────────────────────┘
```

**Nodes:**
- `agent` — renders system prompt (taste + memory summaries substituted), trims history to `max_history_messages`, sanitizes orphaned tool calls, calls the LLM, emits one `agent.tool_decision` reasoning step per turn.
- `tools` — LangGraph `ToolNode`; dispatches to whichever tool(s) the LLM selected.
- `fallback` — fires when `steps_taken >= max_steps` or `error_count >= max_errors`; emits a graceful terminal message and a debug diagnostic step.

**Routing (`should_continue`):** `error_count >= max_errors` → fallback; `steps_taken >= max_steps` → fallback; last message has tool_calls → tools; otherwise → END.

### Per-Turn State

State is a `TypedDict` (`AgentState`). LangGraph persists it via the Postgres checkpointer after every node execution.

| Field | Reducer | Reset per turn? | Notes |
|---|---|---|---|
| `messages` | `add_messages` (append) | No — accumulates | Trimmed to `max_history_messages` (default 40) before each LLM call |
| `taste_profile_summary` | overwrite | Yes — refreshed from DB | Injected by ChatService each turn |
| `memory_summary` | overwrite | Yes — refreshed from DB | Injected by ChatService each turn |
| `user_id` | overwrite | Yes | Used as checkpointer `thread_id`; isolates history per user |
| `location` | overwrite | Yes | `{lat, lng}` or None |
| `last_recall_results` | overwrite | Yes — reset to None | Set by `recall_tool`; read by `consult_tool` in the same turn |
| `reasoning_steps` | overwrite | Yes — reset to `[]` | Accumulated within a turn; returned to caller |
| `steps_taken` | overwrite | Yes — reset to 0 | Incremented by `agent_node`; bounds the loop |
| `error_count` | overwrite | Yes — reset to 0 | Incremented by tool error handlers; bounds the loop |

### Conversation History

The Postgres checkpointer (`AsyncPostgresSaver` backed by `AsyncConnectionPool`) persists the full `AgentState` keyed by `thread_id = user_id`. Conversation history accumulates across sessions. Before each LLM call, `agent_node` trims to the last `max_history_messages` messages (default 40, configurable in `app.yaml`). Trim runs **before** `_sanitize_orphaned_tool_calls` — this ordering ensures a `ToolMessage` never lands at position 0 of the trimmed slice, which would cause a 400 from Anthropic.

### Tools

All three tools are `@tool`-decorated async functions built by factory functions (`build_recall_tool`, `build_consult_tool`, `build_save_tool`). Each tool uses `Annotated[AgentState, InjectedState]` and `Annotated[str, InjectedToolCallId]` for LangGraph injection — no `args_schema=` passed to `@tool` (that short-circuits LangGraph's injection inspection).

Every tool wraps its body in `with_timeout(tool_name, ...)` which enforces the per-tool timeout from `app.yaml` (`tool_timeouts_seconds.recall/consult/save`) and returns a degraded `Command` on timeout or unhandled exception (increments `error_count`, emits a user-visible `tool.summary` step). `NodeInterrupt` and `GraphInterrupt` are re-raised — they are LangGraph control-flow signals.

| Tool | Trigger | Reads from state | Writes to state | Writes to DB |
|---|---|---|---|---|
| `recall` | User references their saves, or consult precondition | `user_id`, `location`, `last_recall_results` (none) | `last_recall_results` (list of PlaceObjects) | Nothing |
| `save` | User shares a URL or names a place | `user_id` | `reasoning_steps`, `messages` | `places` + `embeddings` (via ExtractionService) |
| `consult` | User asks for a recommendation | `user_id`, `location`, `last_recall_results` (from prior recall in same turn) | `reasoning_steps`, `messages` | `recommendations` table (via ConsultService — ADR-060) |

**recall → consult handoff:** When the agent calls both in one turn, `recall_tool` sets `state["last_recall_results"]`. `consult_tool` reads it from state directly — the LLM does not need to pass the list explicitly; LangGraph threads it automatically.

**save + NodeInterrupt:** When extraction returns a `needs_review` result, `save_tool` raises `NodeInterrupt`. LangGraph checkpoints the state and suspends the graph. The caller receives `type="interrupt"` and the low-confidence candidates for user confirmation. Resuming with confirmation re-enters the graph at the interrupted node.

### Reasoning Steps

Every tool wrapper uses the `build_emit_closure(tool_name)` / `append_summary(...)` helpers from `core/agent/tools/_emit.py`. The pattern:

1. Each service-internal milestone calls `emit(step, summary)` — appends a `debug`-visibility `ReasoningStep` to a local `collected` list and forwards it to `get_stream_writer()` for SSE fan-out.
2. After the service returns, `append_summary(collected, tool_name, summary)` appends one `user`-visible `tool.summary` step.
3. The full `collected` list is merged into `state["reasoning_steps"]` via the `Command` update.

`agent_node` emits one `agent.tool_decision` step per LLM call (visibility `user`). `fallback_node` emits one `fallback` step (visibility `user`) plus one debug diagnostic step (`max_steps_detail` or `max_errors_detail`).

Callers filter `reasoning_steps` by `visibility` to decide what reaches the product-facing JSON payload vs what stays in Langfuse/SSE debug streams.

### Streaming

`POST /v1/chat/stream` runs the same graph via `graph.astream(payload, config, stream_mode="custom")`. The `get_stream_writer()` writer acquired inside each node/tool fans out `ReasoningStep` dicts as SSE events in real time. The final `ChatResponse` is emitted as the last SSE event.

## API Contract

| Endpoint                          | Request                               | Response                                              |
| --------------------------------- | ------------------------------------- | ----------------------------------------------------- |
| POST /v1/chat                     | user_id, message, optional location   | type, message, optional data payload (ADR-052)        |
| GET /v1/extraction/{request_id}   | —                                     | ExtractPlaceResponse (results, source_url, request_id); 404 while pending or after TTL |
| GET /v1/health                    | —                                     | status, db connectivity                               |

All requests come from NestJS after auth verification. This repo never receives requests directly from the frontend.

## Model Assignments

| Logical Role  | Model                   | Why                                                                  |
| ------------- | ----------------------- | -------------------------------------------------------------------- |
| orchestrator  | claude-sonnet-4-6 (default; selectable at boot via `AGENT_MODEL`, ADR-068) | Strong reasoning for tool calling (LangGraph agent) |
| extractor     | GPT-4o-mini             | NER and subtitle/audio extraction enrichers in the save pipeline |
| embedder      | Voyage 4-lite           | 9.25% better retrieval quality than OpenAI; 1024-dimensional vectors |
| taste_regen   | GPT-4o-mini             | Cost-effective for taste profile summarization |
| vision_frames | GPT-4o-mini             | Frame-level vision extraction for background enricher                |
| transcriber   | whisper-large-v3-turbo (Groq) | Fast multilingual STT; 216x real-time; background audio enricher only (ADR-047) |

Model assignments are config-driven via `config/app.yaml` under the `models:` key. No model names hardcoded in application code.

## Service Configuration

Service-specific parameters are config-driven via `config/app.yaml`:

**Recall Service** (hybrid search tuning):
- `max_results`: Maximum results to return (default 10)
- `rrf_k`: RRF constant for rank weighting (default 60, higher = more weight on top ranks)
- `candidate_multiplier`: Pre-fetch factor (multiplies limit; default 2)
- `min_rrf_score`: Minimum RRF score threshold to filter low-relevance results (default 0.01)

Adjust these values to control result relevance and performance.

**Extraction Service** (cascade tuning):
- Per-request candidate cap lives in code, not config — `DEFAULT_MAX_CANDIDATES = 25` in `core/extraction/service.py`. Callers override per-request via `ExtractionService.run(..., limit=N)`. When an enrichment level produces more candidates than the effective limit, the request is dropped entirely — protects quota and DB from noisy inputs.
- `circuit_breaker_threshold`: failures before circuit opens (default 5)
- `circuit_breaker_cooldown`: seconds before half-open probe (default 900)
- `confidence.base_scores`: per-level base confidence — emoji_regex: 0.95, llm_ner: 0.80, subtitle_check: 0.75, whisper_audio: 0.65, vision_frames: 0.55
- `confidence.corroboration_bonus`: bonus when two independent sources find the same name (default 0.10)
- `confidence.max_score`: ceiling — system never claims perfect certainty (default 0.97)
- `vision.max_frames`: maximum frames sent to vision model (default 5)
- `vision.scene_threshold`: ffmpeg scene change threshold (default 0.3)
- `vision.timeout_seconds`: hard timeout for vision enricher (default 10)
- `whisper.timeout_seconds`: hard timeout for audio enricher (default 8)
- `whisper.audio_format`: audio format for in-memory pipe fallback (default opus)
- `whisper.audio_quality`: audio bitrate for in-memory pipe fallback (default 32k)
- `subtitle.output_dir`: temporary directory for VTT files (default /tmp/subtitles)

## Database Access

This repo connects to the same PostgreSQL instance as the product repo.

Writes:

- places (extracted place records)
- embeddings (generated vectors)
- taste_model (learned user patterns)
- consult_logs (AI recommendation history — ADR-053)
- user_memories (personal facts extracted from chat messages)
- interaction_log (append-only behavioral signal log)

Reads:

- All tables as needed (places, embeddings, taste_model, users for context)

Does not write:

- users, user_settings (product data owned by NestJS)

Schema for AI tables (places, embeddings, taste_model, consult_logs, user_memories, interaction_log) is managed by Alembic in this repo. If NestJS changes the users or user_settings table (via TypeORM), FastAPI must adapt. Database client: SQLAlchemy async + asyncpg.

## Redis

Redis is owned exclusively by this repo. The product repo does not connect to Redis.

Used for:

- LLM response caching
- Session context
- Intermediate agent state

## Design Principles

- extract-place is a level-driven workflow built from `EnrichmentLevel`s plus a shared NER finalizer at the pipeline. Not an agent. No LangGraph. The full pipeline runs as a background asyncio task; the HTTP response returns `pending` immediately. The deep level (subtitle/whisper/vision) runs inline inside the pipeline only when the inline level fails to validate any candidates — no domain event dispatch, no separate handler. NER (`LLMNEREnricher`) is the pipeline's finalizer and runs after every executed level over caption + transcript + supplementary_text in a single LLM call.
- consult is currently a sequential 6-step Python pipeline in `ConsultService` (ADR-050: LangGraph parallelization deferred). If LangGraph is added in the future, Steps 2 (retrieve) and 3 (discover) are the candidates for parallel branches — they are independent and their results merge before validation.
- Each pipeline step passes only the data the next step needs. Do not forward the full Google Places API response, full embedding vectors, or raw validation payloads through downstream steps. Extract the fields needed for ranking and drop the rest.

## Design Patterns

These are structural constraints that define how the system is layered.
They describe what lives where and what crosses which boundary.
Behavioral and implementation patterns live in docs/decisions.md.

### Facade — Route Handlers

Route handlers are the HTTP entry point only. Each handler makes
exactly one service call and returns the result. No SQLAlchemy,
no Redis, no pgvector, no Google Places API calls appear inside
src/kebi/api/routes/. All orchestration lives in
src/kebi/core/.

### Protocol — Swappable Dependencies

Any external dependency lives behind a Python Protocol. Concrete
implementations live in src/kebi/providers/ for cross-cutting
dependencies or inside the relevant src/kebi/core/ module for
domain-specific ones. Service layers, agent nodes, and LangGraph
graphs import the Protocol only. Nothing in core/ imports a concrete
provider class directly.

### Repository — Database Access

All SQLAlchemy code lives in six repository classes:
PlaceRepository, EmbeddingRepository, TasteModelRepository, RecallRepository,
ConsultLogRepository, and UserMemoryRepository.
No ORM queries or raw SQL appear outside these classes. Service
and agent layers call repository methods only.

Each repository is defined as a Python Protocol (abstract interface)
with a concrete SQLAlchemy implementation. For example, PlaceRepository
defines two methods:

- `get_by_provider(provider: str, external_id: str) -> Place | None`
  Fetch a place by provider and external ID for deduplication.
- `save(place: Place) -> Place`
  Insert or update a place with explicit error recovery (try/except/rollback).

RecallRepository defines:

- `hybrid_search(user_id: str, query_vector: list[float] | None, query_text: str, limit: int, rrf_k: int, candidate_multiplier: int, min_rrf_score: float, max_cosine_distance: float) -> list[RecallRow]`
  Hybrid search combining pgvector (cosine similarity) and FTS (full-text search) with RRF merging.
  If query_vector is None, falls back to text-only search.
  Returns results with deterministic match_reason (which methods matched).
- `count_saved_places(user_id: str) -> int`
  Count user's saved places for cold-start detection.

ConsultLogRepository defines:

- `save(log: ConsultLog) -> None`
  Persist a consult recommendation record (ADR-053). Called by ConsultService after
  building the response; write failures are logged and not raised.

UserMemoryRepository defines:

- `save(user_id: str, memory: str, source: str, confidence: float) -> None`
  Persist an extracted personal fact. Duplicates silently skipped by UNIQUE constraint.
- `load(user_id: str) -> list[str]`
  Load all stored memory strings for a user.

The hybrid_search query is a single PostgreSQL CTE (Common Table Expression) with:
- vector_results CTE: pgvector cosine distance ranking (if query_vector provided)
- text_results CTE: FTS ts_rank scoring (keyword matching on place_name + cuisine)
- combined CTE: FULL OUTER JOIN merging both result sets
  RRF score: 1/(k + vector_rank) + 1/(k + text_rank), where k=60
- Final SELECT: top limit results by RRF score, with match_reason derived from which CTEs matched

The Protocol lives in src/kebi/db/repositories/ alongside the
concrete SQLAlchemyRecallRepository implementation. Service layers
depend on the Protocol only, not the concrete class. This allows
testing with mock repositories and swapping implementations without
touching service code. All database writes include try/except blocks
with explicit rollback and structured error logging.

### Taste Model

The taste model builds a per-user preference profile from behavioral signals (save, accept, reject, onboarding). Signals aggregate into `signal_counts`; an LLM regen job produces a natural-language `taste_profile_summary` and a structured `chips` array (ADR-058). Chips have a lifecycle — `pending` → `confirmed` / `rejected` via `POST /v1/signal` with `signal_type=chip_confirm` (ADR-061). Confirmed chips are immutable; rejected chips may resurface as pending when the underlying signal count grows. RankingService was deleted — ConsultService returns candidates unranked and an agent (not yet built) will do selection. Full architecture: [docs/taste-model-architecture.md](taste-model-architecture.md).

### Signal Tier (feature 023)

`derive_signal_tier(signal_count, chips, stages, chip_threshold)` — pure function in `core/taste/tier.py` — computes one of `cold`, `warming`, `chip_selection`, `active` from the user's current state. Stages are config-driven (`config/app.yaml → taste_model.chip_selection_stages: dict[str,int]`); adding a new stage (e.g. `round_4: 200`) requires zero code changes because the function iterates `stages.values()` rather than referencing named keys.

The tier is surfaced on `GET /v1/user/context` (plus the full chip array with `status` + `selection_round`). **The product repo gates tier routing** — it reads `/v1/user/context` and decides whether to call `/v1/consult`. At `cold` and `chip_selection` it renders its own UI and never calls consult. At `warming` and `active` it forwards `signal_tier` as a field on `/v1/chat` / `/v1/consult` requests so consult can apply tier-aware behavior (warming 80/20 discovered/saved candidate-count blend; active-tier rejected-chip filter + confirmed-signal reasoning step). `ConsultResponse` is not extended with any envelope — consult is consult.

## Key Boundaries

- One shared PostgreSQL instance. Ownership split by domain: NestJS manages users and user_settings via TypeORM (`synchronize: true`). Alembic in this repo owns places, embeddings, taste_model, consult_logs, user_memories, interaction_log.
- **Critical constraint**: Embedding vector dimensions must stay in sync — but pgvector columns are fully owned by this repo's Alembic migrations. NestJS never touches vector columns. This repo uses Voyage 4-lite with 1024-dimensional embeddings.
- Redis is FastAPI-only.
- Google Places API is called directly by this repo as part of the AI pipeline.
- All LLM, embedding, and transcription provider calls happen in this repo only (OpenAI, Anthropic, Groq, Voyage AI).

## Technology Stack

| Layer           | Technology                     | Notes                                                                 |
| --------------- | ------------------------------ | --------------------------------------------------------------------- |
| Runtime         | Python 3.11                    | AI library compatibility                                              |
| Package Manager | Poetry                         |                                                                       |
| HTTP Layer      | FastAPI 0.115                  | Async, Pydantic-native                                                |
| Agent Framework | LangGraph 0.3                  | Multi-step agent orchestration (deferred for consult — ADR-050)       |
| Chains          | LangChain 0.3                  | Document loaders, retrievers, chains                                  |
| LLM Providers   | OpenAI, Anthropic, Groq        | Via provider abstraction layer; roles mapped in `config/app.yaml`     |
| Extraction / Vision | GPT-4o-mini (OpenAI)       | Structured extraction, vision enricher                                |
| Orchestration   | claude-sonnet-4-6 (Anthropic)  | Strong reasoning for agent/orchestration layer                        |
| Embeddings      | voyage-4-lite (Voyage AI)      | 1024-dimensional vectors; 32k token context window                    |
| Transcription   | whisper-large-v3-turbo (Groq)  | Multilingual STT for background audio enricher (ADR-047)              |
| Structured Output | Instructor 1.0               | LLM output parsing via OpenAI-compatible function calling             |
| Monitoring      | Langfuse 2.0                   | LLM tracing, monitoring, and evaluation                               |
| Cache           | Redis 5.0                      | LLM response caching, extraction status, agent state                  |
| Database Client | SQLAlchemy 2.0 async + asyncpg | Read-write connection to PostgreSQL + pgvector                        |
| Migrations      | Alembic 1.14                   | Manages AI tables: places, embeddings, taste_model, consult_logs, user_memories |
| External API    | Google Places API              | Place validation and nearby discovery                                 |
| Media Extraction | yt-dlp                        | Video metadata and audio extraction for TikTok/YouTube enrichers      |
| Deploy          | Railway                        |                                                                       |
| Local Dev       | Docker Compose                 | PostgreSQL + pgvector, Redis, FastAPI                                 |
