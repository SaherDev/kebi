# System Architecture — Kebi

This document is a high-level boundary and dataflow reference. It describes the **current** shape of the service: what this repo owns, the live routes, and the agent and extraction shapes the product repo calls. For the rationale behind every choice, see [`decisions.md`](decisions.md). For the wire contract, see [`api-contract.md`](api-contract.md).

## Overview

Kebi is the AI engine of the Kebi product. It owns all AI logic: natural language understanding, place extraction, embedding generation, vector retrieval, agent orchestration, ranking, and taste modeling. It runs as a standalone FastAPI service that the product repo (NestJS + Next.js) calls over HTTP.

```
┌──────────────────────────────────┐
│   product repo                   │
│   NestJS backend                 │
│   Auth-verifies and forwards     │
└───────────────┬──────────────────┘
                │ HTTP (JSON, SSE)
                ▼
┌─────────────────────────────────────────────────────────────┐
│                  kebi (this repo)                            │
│                                                              │
│  FastAPI HTTP layer (routes under /v1/)                      │
│  LangGraph agent (orchestrator + consult-family tools)       │
│  Extraction pipeline (level-driven, deterministic)           │
│  Provider abstraction (LLM, embedding, transcription)        │
└──┬────────────────┬────────────────┬────────────────┬───────┘
   │ SQL            │ HTTPS          │ HTTPS          │ TCP
   ▼                ▼                ▼                ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ PostgreSQL   │ │ Google       │ │ LLM / STT /  │ │ Redis        │
│ + pgvector   │ │ Places API   │ │ Embedding    │ │ (cache,      │
│              │ │              │ │ providers    │ │  agent state)│
│ AI tables    │ │ Validate +   │ │              │ │              │
│ (Alembic)    │ │ search       │ │ Anthropic    │ │              │
│              │ │              │ │ OpenAI       │ │              │
│              │ │              │ │ Groq         │ │              │
│              │ │              │ │ Voyage AI    │ │              │
└──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘
```

## What This Repo Owns

- Natural language understanding (intent shape, location, mobility scope)
- Place extraction from URLs, free text, and screenshots
- Embedding generation and vector similarity search
- External place discovery and validation (Google Places API)
- Ranking and scoring (deterministic, tunable)
- Taste model construction and reading
- Agent orchestration (LangGraph) for conversational turns
- Redis caching around LLM work (extraction results by URL, web-search results, query embeddings, place payloads; the home suggestion is the one cached LLM output)
- Evaluation pipelines (retrieval accuracy, agent task completion, cost, latency)

## What This Repo Does NOT Do

- Serve UI — no HTML, no templates, no static files
- Manage auth — the product repo authenticates and forwards `user_id` (trusted-upstream model)
- Own product-data migrations — `users` and `user_settings` are owned by NestJS / TypeORM
- Write user records, settings, or anything that isn't AI-generated data

## Live Routes

All routes live under `/v1/`. The full request/response contract is in [`api-contract.md`](api-contract.md).

| Endpoint                          | Purpose                                      |
| --------------------------------- | -------------------------------------------- |
| `POST /v1/chat`                   | Conversational entry point (agent turn)      |
| `POST /v1/chat/stream`            | SSE streaming variant of chat                |
| `POST /v1/extract`                | Canonical save-a-place pipeline              |
| `POST /v1/signal`                 | Behavioral signal log (accept / reject)      |
| `DELETE /v1/user/{user_id}/data`  | Account-deletion sweep of AI-owned data      |
| `GET /v1/health`                  | Liveness + DB probe                          |

## Agent — Conversational Turns

The agent answers `/v1/chat` and `/v1/chat/stream`. It is a LangGraph state machine driven by an LLM in the `orchestrator` role. Each turn:

1. **Resolves a working location** — the place the turn operates against. It is the explicit place named in the message, a place carried from the conversation, or the user's actual coordinates as fallback. When the user has clearly travelled since the previous turn, the resolver re-anchors to the new actual location.
2. **Resolves a search scope** — an effective movement mode (walking / cycling / motorbike / driving / transit / rideshare) paired with the user's mobility capability and a search radius derived from the location's density and the user's reach setting.
3. **Selects a tool from the consult family** (or none) and synthesises a final answer.

The consult-family tools share a single argument schema so routing happens on intent semantics, not on parameter shape:

| Tool              | Role                                                                   |
| ----------------- | ---------------------------------------------------------------------- |
| `find_saved`      | Search the user's own saved places (hybrid pgvector + FTS, RRF merge)  |
| `suggest_places`  | LLM-named candidates validated against the place provider              |
| `discover_places` | Direct provider lookup for utility intents (pharmacy, ATM, supermarket) and as a fall-through when the other two return empty |

Hard constraints from the user's memory (dietary, accessibility) are applied identically across all three tools at the filter layer; the LLM cannot bypass them. The agent composes the final per-pick reason from taste profile + memory + working location — the structured `reason` field on the candidate envelope is the namer's per-candidate rationale only, and the agent's user-facing prose is what the user sees.

A per-turn tool-call budget caps how many tools the agent may invoke in a single turn; exceeding it surfaces a dedicated "give me a bit more detail" message rather than a generic error.

The full reasoning behind the location / scope / tool-family design lives in ADR-083, ADR-084, ADR-086, ADR-088, ADR-089, ADR-090, ADR-091.

## Two Layers the Agent Reads

Every answer draws on two separate stores. Neither knows about the agent's mode; the agent is the only thing that reads both together.

- **Knowledge layer** — what exists in the world: claims about countries, cities, neighborhoods, and places, held in the `knowledge_claims` table (ADR-120). One row per claim, entity-scoped via a canonical `entity_key` (`place:<places.id>` for places, a hierarchical geo slug like `ae/dubai/jumeirah` for country/city/neighborhood — no bare-name collisions). A `source_type` generalizes every origin behind the same shape: content harvested from shared posts, curated traveler expertise, and claims surfaced in conversation. Global claims (`shared_content`, `curated_expert`) are visible to everyone; conversation-origin claims (`kebi_message`, `user_message`) carry the speaker's `user_id` and are only ever read back for that same user. This sits alongside — not instead of — `places.tags` (ADR-118), which stays the fast, provenance-light vocabulary ranking already reads; `knowledge_claims` is the provenance-bearing substrate underneath it.
- **Taste layer** — what *this user* cares about: the append-only `interactions` log is the source of truth, `taste_model` is its LLM-regenerated derived profile, and `user_memories` holds declarative personal facts stated in chat ("I'm vegetarian"). This layer is unchanged by ADR-120 — it is named here only as the counterpart the knowledge layer completes.

Knowledge tells the agent what is true; taste tells it what is relevant to the person asking.

## Extraction — Saving a Place

Extraction is a deterministic, level-driven pipeline. There is **no agent** in the save path; the product repo calls `POST /v1/extract` directly with a URL or place name.

```
POST /v1/extract  (synchronous)
    │
    ├── Canonical URL cache lookup (Redis)
    │   HIT  → link cached places to user, return (~50 ms)
    │   MISS → fall through to pipeline
    │
    └── Level-driven pipeline
        │  Each level is a group of producers (text / signal enrichers).
        │  Producers are source-filtered: an enricher with no support for
        │  the URL's source short-circuits.
        │
        ├── Inline level — fast text enrichers (caption / oEmbed metadata)
        ├── Deep level   — subtitles, audio transcription, vision frames
        │                  (runs only if inline yields no validated candidates)
        ├── Finalizer    — one consolidated NER pass over all collected text
        ├── Cap check    — too many candidates → drop the request entirely
        │                  (protects Google / DB / embeddings quotas)
        ├── Validate     — Google Places, parallel fan-out
        └── Persist      — every validated candidate lands in user_places
                           with approved=False; the user curates later
                         → write to canonical-URL cache for cross-user reuse
                         → write evidence ledger to object storage
                         → return ExtractPlaceResponse
```

The pipeline returns its envelope synchronously — there is no polling route, no background job ID. The audit trail of which enrichers produced which snippets is written to an append-only object-storage ledger, not the response, so the wire contract does not leak pipeline internals.

For rationale: ADR-070 (single place-search service), ADR-071 (every picker output saved), ADR-073 (extraction is HTTP-only), ADR-074 (canonical-URL cache), ADR-093 (evidence ledger), ADR-094 (no polling route).

## Database Access

One shared PostgreSQL instance on Railway. Ownership splits cleanly by domain.

**This repo writes** (Alembic-owned migrations):

- `places` — shared place catalog (one row per real-world place)
- `place_embeddings` — pgvector vectors for similarity search
- `user_places` — per-user save links (with the `approved` curation flag)
- `taste_model` — per-user taste profile (signal counts + LLM-regenerated summary)
- `interactions` — append-only behavioral signal log
- `user_memories` — personal facts extracted from chat messages
- `knowledge_claims` — entity-scoped world-knowledge claims (ADR-120)

**This repo does not write:** `users`, `user_settings` — those are NestJS / TypeORM.

**Dropped tables:** the legacy v1 `places` / `embeddings` shape and the `recommendations` table were removed (ADR-078). Signal validation no longer requires a recommendation row.

All SQLAlchemy queries live in repository classes — services and agent nodes never see ORM models. Repositories are defined as Python Protocols with concrete implementations, so service-layer code is testable against in-memory fakes.

## Provider Abstraction

Every external dependency lives behind a Python Protocol. LLM, embedding, transcription, and place-provider concrete adapters live in `providers/`. Service layers import the Protocol only — concrete provider classes never appear in `core/`.

Logical model roles map to provider + model + params in `config/app.yaml` under `models:`. Current roles include: `orchestrator`, `extractor`, `embedder`, `transcriber`, `vision_frames`, `taste_regen`, `memory_extractor`, `location_resolver`, `candidate_namer`. Code references roles, not model names — swapping a model is a config change.

See ADR-026 for the rationale, and `config/app.yaml` for the live mapping.

## Redis

Redis is owned exclusively by this repo; the product repo does not connect to it. Uses:

- Extraction result cache (keyed by canonical URL, 30-day TTL — ADR-074) — skips the whole pipeline including its LLM calls
- Home suggestion cache (the one true LLM-response cache — ADR-111)
- Web-search result cache (ADR-145), query-embedding cache, place-payload cache
- LangGraph checkpointer state for agent conversation history
- Rate-limit and circuit-breaker counters

## Cost and Tracing

Langfuse traces every paid call (LLM, embedding, transcription, place-provider, third-party scrapers) via manual spans — there is no LangChain callback handler; each call site opens a span and stamps token usage on it. One parent trace opens per user-facing entry point and every nested paid call attaches to it. Per-tool attribution comes from a contextual stamp set on tool entry. All pricing lives in `app.yaml` (`pricing:`): LLM calls are priced from `pricing.llm` (cache-aware, keyed by model name) and stamped as span cost, with Langfuse's own catalog as the cross-check; per-call providers (Google Places, Apify, Whisper, Voyage) are priced the same way at the call site. Retries are explicit: SDK-internal retries are disabled, retry loops are kebi-owned with one span per attempt, and Instructor validation retries surface as `llm_attempts` on the span. `scripts/llm_cost_report.py` aggregates per-role calls/tokens/cost/latency from the Langfuse API. Reconciliation is monthly: Langfuse totals vs. provider invoices; drift triggers a rate update. See ADR-025, ADR-092, ADR-172.

## Boundaries (quick reference)

- One shared PostgreSQL instance; ownership split by domain (above).
- Embedding vector dimensions: 1024 (Voyage 4-lite). This repo owns pgvector columns end-to-end.
- Redis is FastAPI-only.
- Google Places, Anthropic, OpenAI, Groq, Voyage AI, and any third-party scraper are called only from this repo.
- The product repo never receives requests directly from the frontend that should hit kebi — NestJS is the trusted upstream.

## Technology Stack

| Layer                | Choice                            |
| -------------------- | --------------------------------- |
| Runtime              | Python 3.11                       |
| Package manager      | Poetry                            |
| HTTP                 | FastAPI                           |
| Agent framework      | LangGraph                         |
| LLM providers        | Anthropic, OpenAI                 |
| Embeddings           | Voyage AI (voyage-4-lite, 1024-d) |
| Transcription        | Groq (whisper-large-v3-turbo)     |
| Structured output    | Instructor                        |
| Tracing              | Langfuse                          |
| Cache + agent state  | Redis                             |
| Database client      | SQLAlchemy 2.0 async + asyncpg    |
| Migrations           | Alembic                           |
| Object storage       | S3-compatible (ADR-093 evidence)  |
| Deploy               | Railway                           |
| Local dev            | Docker Compose                    |
