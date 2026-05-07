# kebi

The AI engine behind [Kebi](https://github.com/SaherDev/kebi-app).

> Google Maps and Yelp return 50 options. People want one.

Kebi replaces browse-based place discovery with a single intent-driven consultation. Users save places over time, the system builds a behavioral taste profile, and when they state intent like "cheap dinner nearby" it returns one confident recommendation.

This repo is the AI brain. It owns place extraction, embedding generation, hybrid RAG retrieval, deterministic ranking, taste modeling, and LangGraph agent orchestration (a Claude Sonnet agent dispatches `recall`, `save`, and `consult` tools). The product repo (NestJS + Next.js) calls this service over HTTP and handles auth, UI, and recommendation history. See [docs/api-contract.md](docs/api-contract.md) for the full contract.

**Stack:** Python 3.11, FastAPI, LangGraph, LangChain, PostgreSQL + pgvector, Redis, Claude Sonnet (agent orchestration), GPT-4o-mini (taste regeneration, vision, memory extraction), Voyage AI (embeddings), Groq Whisper (transcription), Langfuse (observability), Railway (deploy).

**Status:** Shipped to production, solo-built. ~12k lines of Python, 71 tests.

## Architecture

Every conversational request enters through `POST /v1/chat`. A Claude Sonnet agent — built on LangGraph with LangChain message primitives — decides which of three tools to invoke:

- **`recall`** — hybrid RAG over the user's saved places (pgvector + metadata filters).
- **`save`** — place extraction from free text or URLs, validated against Google Places.
- **`consult`** — fresh discovery via Google Places, scored by the ranking layer against the user's taste model.

Results return as JSON, or stream as Server-Sent Events through `POST /v1/chat/stream` (reasoning steps first, final result last).

```text
    Client  (product repo: Next.js + NestJS)
       │
       │   POST /v1/chat     (or /v1/chat/stream for SSE)
       ▼
    FastAPI
       │
       ▼
    LangGraph Agent  ·  Claude Sonnet
       │   picks one tool per turn
       │
       ├──▶  recall    hybrid RAG over the user's saved places
       ├──▶  save      place extraction from text or URLs
       └──▶  consult   Google Places discovery
                           │
                           ▼
                       Ranking  ◀──  Taste model (EMA per user)
                           │
                           ▼
              PostgreSQL + pgvector  ·  Redis  ·  Langfuse
                           │
                           ▼
                  Response  (JSON or SSE stream)
```

Full data flows live in [docs/architecture.md](docs/architecture.md). Design rationale for each layer is in [docs/decisions.md](docs/decisions.md) (ADRs).

## Modules

Domain surface under `src/kebi/core/`:

| Module        | Responsibility                                                          |
| ------------- | ----------------------------------------------------------------------- |
| `agent/`      | LangGraph + LangChain agent, tool dispatch (`recall`/`save`/`consult`), SSE stream |
| `extraction/` | Free text + URLs → structured place data (Google Places validation)    |
| `places/`     | `PlaceObject` model, three-tier storage (Postgres + Redis geo + enrich) |
| `recall/`     | Hybrid RAG retrieval over the user's saved places                       |
| `consult/`    | Discovery (Google Places) + ranking for intent-driven recommendations   |
| `ranking/`    | Deterministic scoring against taste model and context                   |
| `taste/`      | EMA-updated taste profile, regeneration via GPT-4o-mini                 |
| `memory/`     | User memory extraction and retrieval                                    |
| `signal/`     | Chip selection, onboarding signal tier                                  |

## Docs

| Doc                                                                  | What's in it                                                    |
| -------------------------------------------------------------------- | --------------------------------------------------------------- |
| [docs/architecture.md](docs/architecture.md)                         | System overview, data flows, model assignments, design patterns |
| [docs/api-contract.md](docs/api-contract.md)                         | HTTP contract between NestJS and this service                   |
| [docs/decisions.md](docs/decisions.md)                               | Architecture decision records (ADRs) — read before implementing |
| [docs/taste-model-architecture.md](docs/taste-model-architecture.md) | Taste model dimensions, EMA update formula, ranking integration |

## Endpoints

| Route                                | Purpose                                                                                                 |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------- |
| `POST   /v1/chat`                    | Unified conversational entry — agent dispatches recall/save/consult                                     |
| `POST   /v1/chat/stream`             | Same as `/v1/chat`, streamed as SSE (reasoning steps + final result)                                    |
| `POST   /v1/signal`                  | Behavioral signals — recommendation accept/reject, chip confirm                                         |
| `GET    /v1/user/context`            | Signal tier + onboarding chips (drives tier routing in the product)                                     |
| `DELETE /v1/user/{user_id}/data`     | Erase user's AI-owned data. Optional repeated `?scope=` query: `all` (default — full wipe used by NestJS account-delete) or `chat_history` (clears just the agent's checkpoint thread, leaves saves intact). Unknown values → 422 |
| `GET    /v1/extraction/{request_id}` | Poll status for background place extractions                                                            |
| `GET    /v1/health`                  | Service health check                                                                                    |

## Setup

**Prerequisites:** Python 3.11, Poetry, Docker.

```bash
# 1 — Install Python dependencies
poetry install

# 2 — Create your local .env (fill in the keys listed under Environment Variables)
cp .env.example .env

# 3 — Start PostgreSQL and Redis
docker compose up -d

# 4 — Apply database migrations
poetry run alembic upgrade head

# 5 — Run the API with hot reload
poetry run uvicorn kebi.api.main:app --reload
```

Verify the service is up: `curl http://localhost:8000/v1/health` → `{"status": "ok", "db": "connected", ...}`

## Environment Variables

Secrets go in `.env` at the project root (gitignored). Copy `.env.example` to get started.

| Variable              | Required | Description                                                             |
| --------------------- | -------- | ----------------------------------------------------------------------- |
| `DATABASE_URL`        | yes      | PostgreSQL connection URL                                               |
| `REDIS_URL`           | yes      | Redis connection URL                                                    |
| `OPENAI_API_KEY`      | yes      | OpenAI — taste regeneration, vision frames, memory extraction, evals    |
| `ANTHROPIC_API_KEY`   | yes      | Anthropic — agent orchestrator (Claude Sonnet)                          |
| `VOYAGE_API_KEY`      | yes      | Voyage AI — embeddings (voyage-4-lite)                                  |
| `GOOGLE_API_KEY`      | yes      | Google Places API — place validation and discovery                      |
| `GROQ_API_KEY`        | yes      | Groq — transcription (whisper-large-v3-turbo)                           |
| `LANGFUSE_PUBLIC_KEY` | yes      | Langfuse — LLM tracing                                                  |
| `LANGFUSE_SECRET_KEY` | yes      | Langfuse secret                                                         |
| `LANGFUSE_HOST`       | yes      | Langfuse host URL                                                       |
| `APIFY_TOKEN`         | no       | Apify Personal API token — enables Google Maps shared-list import via the `parseforge/google-maps-shared-list-scraper` actor. When unset, Maps list URLs short-circuit silently with no candidates. Get one at [console.apify.com](https://console.apify.com/account/integrations) (free tier covers ~hundreds of list pulls/month). |

Non-secret config (model assignments, extraction weights, service tuning) lives in `config/app.yaml`.

## Commands

```bash
poetry run pytest                          # run tests
poetry run pytest -x                       # stop on first failure
poetry run ruff check src/ tests/          # lint
poetry run mypy src/                       # type check
poetry run alembic revision --autogenerate -m "description"   # new migration
poetry run alembic upgrade head            # apply migrations
```
