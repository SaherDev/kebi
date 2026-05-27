# kebi

The AI engine behind [Kebi](https://github.com/SaherDev/kebi-app).

> "Knows a guy." "I got you." "One spot. Trust me."

Your places live scattered across five apps that don't talk to each other — a TikTok save here, a Maps star there, a screenshot, a name you texted yourself. Three weeks later you're standing somewhere hungry and none of it is findable. And the apps that *do* recommend don't know you — they know the crowd, the algorithm, the trending list. ChatGPT doesn't know where you are or what you've saved either, so it gives you the same generic answer it gives everyone.

Kebi is the opposite. Context on one side, tools on the other, an agent in the middle that holds your history, understands your taste, knows your constraints, and answers with one confident pick instead of a list to scroll.

This repo is the AI brain. It owns:

- **One save, anywhere** — TikTok / Instagram / YouTube URLs, Google Maps lists, free text. The pipeline figures out what it is, enriches it against the place provider, and persists it as a tentative save the user curates later.
- **A behavioral taste profile** — every save and every accept/reject feeds a profile the system regenerates as plain-English summary, not opaque vectors. Revealed preferences, not a questionnaire.
- **User memory** — hard constraints the user states in conversation (dietary, accessibility, "usually with my partner") and softer hints, stored with confidence so the agent knows which to enforce vs which to weight.
- **A conversational agent** — resolves the working location and search scope per turn, then picks at most one tool from the consult family: their own saves, LLM-named candidates validated against the place provider, or direct provider discovery for utility intents.
- **An evidence ledger** — every extraction writes an append-only audit trail keyed by place, so accumulated context about a place across many posts is available for future research-style answers.

The product repo (NestJS + Next.js) calls this service over HTTP and handles auth, UI, and recommendation history. See [docs/api-contract.md](docs/api-contract.md) for the full contract.

**Stack:** Python 3.11, FastAPI, LangGraph, LangChain, PostgreSQL + pgvector, Redis, Claude Sonnet (agent orchestrator), GPT-4o-mini (extraction, taste regen, vision, memory), Voyage AI (embeddings), Groq Whisper (transcription), Langfuse (tracing + cost), Railway (deploy + object storage).

**Status:** Shipped to production, solo-built.

## Architecture

Two entry shapes:

- **Conversation** — `POST /v1/chat` (and the SSE variant `POST /v1/chat/stream`). A Claude Sonnet agent resolves a per-turn working location and search scope, then picks at most one tool from the consult family and composes the answer.
- **Save a place** — `POST /v1/extract`. A deterministic, level-driven pipeline (no agent, no tool selection) turns a URL or free text into structured places, validates them against Google Places, and persists them as tentative saves the user curates later.

The consult-family tools share one argument schema so the agent routes on intent semantics, not parameter shape:

- **`find_saved`** — search the user's own saved places (hybrid pgvector + FTS + Reciprocal Rank Fusion).
- **`suggest_places`** — LLM-named candidates validated against Google Places (catches "famous spots for X" intents).
- **`discover_places`** — direct provider lookup for utility intents (pharmacy, ATM, supermarket) and as a fall-through.

Hard constraints from user memory (dietary, accessibility) apply identically across all three tools at the filter layer — the LLM cannot bypass them.

```text
    Client  (product repo: Next.js + NestJS)
       │
       │   POST /v1/chat            POST /v1/extract
       │   POST /v1/chat/stream
       ▼                            ▼
    FastAPI                       FastAPI
       │                            │
       ▼                            ▼
    LangGraph Agent              Extraction pipeline
       │  resolve location +        │  canonical-URL cache
       │  search scope              │  level-driven enrichers
       │  pick a tool               │  Google Places validation
       │                            │  evidence ledger
       ├──▶  find_saved             ▼
       ├──▶  suggest_places      ExtractPlaceResponse
       └──▶  discover_places
                 │
                 ▼
        PostgreSQL + pgvector  ·  Redis  ·  Langfuse  ·  Object Storage
                 │
                 ▼
        ChatResponse  (JSON or SSE stream)
```

Boundary and dataflow details live in [docs/architecture.md](docs/architecture.md). Design rationale is in [docs/decisions.md](docs/decisions.md).

## Modules

Domain surface under `src/kebi/core/`:

| Module        | Responsibility                                                          |
| ------------- | ----------------------------------------------------------------------- |
| `agent/`      | LangGraph agent, working-location + scope resolver, consult-family tools, SSE stream |
| `chat/`       | Chat service entry point — payload assembly, taste/memory injection     |
| `extraction/` | URLs + free text → structured places, level-driven pipeline             |
| `places/`     | Shared place catalog: search service, upserts, repos, hybrid search     |
| `taste/`      | Signal aggregation, LLM taste-profile summary, regeneration             |
| `memory/`     | User memory extraction and summary                                      |
| `signal/`     | Recommendation accept/reject behavioral signals                         |
| `user/`       | Account-deletion sweep of AI-owned data                                 |

## Docs

| Doc                                                                  | What's in it                                                    |
| -------------------------------------------------------------------- | --------------------------------------------------------------- |
| [docs/architecture.md](docs/architecture.md)                         | Boundary, dataflow, module map, DB write split                  |
| [docs/api-contract.md](docs/api-contract.md)                         | HTTP contract between NestJS and this service                   |
| [docs/decisions.md](docs/decisions.md)                               | Architecture decision records — read before implementing        |
| [docs/taste-model-architecture.md](docs/taste-model-architecture.md) | Signal aggregation, regen flow, agent read path                 |
| [docs/runbooks/](docs/runbooks/)                                     | Operational runbooks (cost reconciliation, …)                   |

## Endpoints

| Route                            | Purpose                                                                                                 |
| -------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `POST   /v1/chat`                | Conversational turn — agent picks a consult-family tool and composes the answer                         |
| `POST   /v1/chat/stream`         | SSE variant — reasoning steps, tool results, then the final message                                     |
| `POST   /v1/extract`             | Canonical save-a-place pipeline (URL or place name → tentative saves)                                   |
| `POST   /v1/signal`              | Behavioral signal — recommendation accept/reject                                                        |
| `DELETE /v1/user/{user_id}/data` | Erase a user's AI-owned data. Optional repeated `?scope=`: `all` (default) or `chat_history`. Unknown → 422 |
| `GET    /v1/health`              | Liveness + DB probe                                                                                     |

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
| `OPENAI_API_KEY`      | yes      | OpenAI — extraction, taste regen, vision, memory extraction             |
| `ANTHROPIC_API_KEY`   | yes      | Anthropic — agent orchestrator (Claude Sonnet)                          |
| `VOYAGE_API_KEY`      | yes      | Voyage AI — embeddings (voyage-4-lite)                                  |
| `GOOGLE_API_KEY`      | yes      | Google Places API — place validation and discovery                      |
| `GROQ_API_KEY`        | yes      | Groq — transcription (whisper-large-v3-turbo)                           |
| `LANGFUSE_PUBLIC_KEY` | yes      | Langfuse — LLM tracing and cost attribution                             |
| `LANGFUSE_SECRET_KEY` | yes      | Langfuse secret                                                         |
| `LANGFUSE_HOST`       | yes      | Langfuse host URL                                                       |
| `APIFY_TOKEN`         | no       | Apify Personal API token — enables Google Maps shared-list import. When unset, Maps list URLs short-circuit silently. Get one at [console.apify.com](https://console.apify.com/account/integrations) |
| `BUCKET_ENDPOINT_URL` | no       | S3-compatible object-storage endpoint for the extraction evidence ledger. When unset, evidence writes degrade to a no-op (extraction still succeeds) |
| `AGENT_MODEL`         | no       | Selects an orchestrator option declared in `config/app.yaml` (default if unset)                                                       |

Non-secret config (model assignments, extraction weights, agent tuning, pricing for cost attribution) lives in `config/app.yaml`.

## Commands

```bash
poetry run pytest                          # run tests
poetry run pytest -x                       # stop on first failure
poetry run ruff check src/ tests/          # lint
poetry run mypy src/                       # type check
poetry run alembic revision --autogenerate -m "description"   # new migration
poetry run alembic upgrade head            # apply migrations
```
