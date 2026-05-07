# Kebi Constitution

## I. Repo Boundary (NON-NEGOTIABLE)

This repo is the autonomous AI brain. It owns everything AI/ML. It never touches UI, auth, user management, or product CRUD. The product repo calls this repo over HTTP only — two endpoints.

This repo owns:

- Intent parsing, place extraction, embeddings, vector search
- Google Places API calls (validation + discovery)
- Writing `places`, `embeddings`, `taste_model` to PostgreSQL
- Ranking, taste modeling, agent orchestration
- LLM provider abstraction, Redis, evaluations

This repo does NOT own:

- Users and user_settings — that's NestJS
- Product-side database migrations — NestJS (TypeORM with `synchronize: true`) manages users and user_settings
- Any UI, auth, payment, or notification logic

## II. Architecture Decisions Are Constraints

All ADRs in `docs/decisions.md` are accepted constraints. A new approach contradicting an existing ADR requires a superseding ADR entry first.

Current binding decisions:

- **ADR-001**: src layout (`src/kebi/`)
- **ADR-002**: Hybrid directory: `api/`, `core/`, `providers/`, `eval/`
- **ADR-003**: Ruff (lint+format) + mypy strict
- **ADR-004**: pytest in `tests/` mirroring `src/` structure
- **ADR-005**: Single `config/models.yaml` for all model config
- **ADR-006**: Python >=3.11,<3.13
- **ADR-008**: extract-place is a sequential async function, NOT a LangGraph graph
- **ADR-009**: Retrieval (pgvector) and discovery (Google Places) run as parallel LangGraph branches
- **ADR-010**: Each LangGraph node passes only fields the next node needs — no full payloads forwarded
- **ADR-011**: Register only the tools the agent needs for the current task — no preloading
- **ADR-014**: `/v1` prefix via `APIRouter`, loaded from `app.yaml` — not hardcoded
- **ADR-015**: `load_yaml_config()` + `find_project_root()` for all config loading
- **ADR-016**: `config/models.yaml` maps logical roles → provider + model + params
- **ADR-017**: Pydantic `BaseModel` for all request/response schemas — no raw dicts at API boundary
- **ADR-018**: Separate router modules: `routes/extract_place.py` and `routes/consult.py`
- **ADR-019**: FastAPI `Depends()` for database session and Redis client
- **ADR-020**: Provider abstraction reads `models.yaml` — code calls `get_llm("intent_parser")`, never hardcodes model names
- **ADR-021**: consult agent uses LangGraph `StateGraph` — compiled once at startup, invoked per request
- **ADR-022**: Google Places client in `core/extraction/places_client.py` with `validate_place()` and `discover_nearby()`
- **ADR-023**: HTTP error mapping: 400 bad input, 422 unparseable/no results, 500 internal failure
- **ADR-024**: Redis LLM response cache keyed by hash of (role, prompt, model, temperature)
- **ADR-025**: Langfuse callback handler attached to every LLM and embedding call — no call goes untraced
- **ADR-044**: Prompt injection mitigation — every LLM call injecting retrieved content must use: (1) defensive system prompt instruction, (2) XML `<context>` tags around retrieved data, (3) Pydantic output validation via Instructor. Constitution Check item for Node 6 and any future content-injecting node.

## III. Provider Abstraction (NON-NEGOTIABLE)

Model names are never hardcoded in code. Always reference logical roles:

- `intent_parser` → currently `openai/gpt-4o-mini`
- `orchestrator` → currently `anthropic/claude-sonnet-4-6`
- `embedder` → currently `voyage/voyage-4-lite`

Swapping a model = one line change in `config/models.yaml`. No code changes.

## IV. Pydantic Everywhere

No raw dicts cross function or module boundaries. All inputs/outputs use Pydantic models. `mypy --strict` is the target. FastAPI returns 422 automatically for malformed requests.

## V. Configuration Rules

- Non-secret config → `config/app.yaml`, `config/models.yaml`
- Secrets → local file `config/.local.yaml` (gitignored, created locally by developers)
- Config loaded via `load_yaml_config(name)` — never hardcoded paths

## VI. Database Write Ownership

- This repo writes: `places`, `embeddings`, `taste_model`, `consult_logs`, `user_memories`, `interaction_log`
- NestJS writes: `users`, `user_settings`
- Alembic in this repo owns all AI-table migrations; TypeORM in the product repo manages product tables
- Embedding dimensions are defined only in this repo's Alembic migrations — NestJS never touches pgvector columns

## VII. Redis Ownership

Redis is exclusively owned by this repo. NestJS never connects to Redis. Use Redis for: LLM response caching, session context, intermediate agent state.

## VIII. API Contract

Three endpoints (ADR-048 added status polling on 2026-04-07):

- `POST /v1/extract-place` — sequential async workflow (not LangGraph)
- `POST /v1/consult` — LangGraph StateGraph agent
- `GET /v1/extract-place/status/{request_id}` — cache-backed status polling for provisional extractions

Full contract in `docs/api-contract.md`. NestJS is the only caller. Frontend never calls this repo directly.

## IX. Testing

- pytest in `tests/` mirroring `src/kebi/` structure
- `mypy --strict` must pass
- `ruff check` must pass
- Every new module gets a corresponding test file

## X. Git & Commits

- Comment char is `;` not `#`
- Format: `type(scope): description #TASK_ID`
- Types: `feat|fix|refactor|test|docs|chore|ci`
- Scopes: module or area (e.g., `intent`, `api`, `providers`, `config`, `ranking`)
- Feature branches from `dev`, merge to `dev`, milestones merge to `main`
- New endpoints need a `.bru` file in `kebi-config/bruno/`

## Governance

This constitution supersedes ad-hoc decisions. New architectural choices require a new ADR in `docs/decisions.md` before implementation. Constitution updated when ADRs are added.

**Version**: 1.0 | **Ratified**: 2026-03-08
