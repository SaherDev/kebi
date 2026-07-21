# CLAUDE.md

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
docker compose up -d                  # start services (PostgreSQL, Redis)
docker compose down -v                # stop services and remove volumes
```

## Project Context

Kebi is the AI engine for the Kebi product — an AI-native place decision engine: users share places over time, the system builds a taste model and returns one confident recommendation from natural-language intent. This repo is pure Python (3.11, Poetry, FastAPI, LangGraph, Pydantic, SQLAlchemy + pgvector, Redis, Alembic, Langfuse); the product repo (NestJS/Next.js) calls it over HTTP only. Model roles, params, and all non-secret config live in `config/app.yaml`. Deployed on Railway.

## Key Directories

- `src/kebi/` — main package (src layout): `api/` routes, `core/` domain modules, `providers/` LLM/embedding abstraction, `db/` models + Alembic migrations + repositories, `eval/` harnesses
- `tests/` — pytest, mirrors src structure (`asyncio_mode = "auto"` — no asyncio markers needed)
- `config/` — `app.yaml`, all non-secret settings (committed)
- `docs/` — architecture, API contract (`api-contract.md`), decisions log (`decisions.md`)
- `scripts/` — utility scripts

## Standards

See @.claude/rules/architecture.md for repo boundaries, DB write ownership, and coding constraints — all binding.

- **Secrets** (ADR-051): root `.env` is a gitignored symlink → `kebi-config/secrets/ai.env.local`. A command failing with a missing API key means check that file, not this repo.
- **Provider abstraction**: code references logical model roles from `config/app.yaml` (`models:`), never model names — swapping a model must be a config-only change.

## Workflow

See `.claude/workflows.md` for the 5-step workflow, constitution check, model assignments, and token costs (source of truth).

- **Read `docs/decisions.md` first** — every ADR is a binding constraint; if your approach contradicts one, stop and flag it before planning or implementing.
- **Write an ADR after each completed task** (next free ADR-NNN in `docs/decisions.md`) — no task is done until it's recorded.
- **Task-driven**: each task arrives scoped — execute it. No phase gates, no phase language.

## Notes

- Git comment char is `;` not `#` (repo git config) — branch/commit/merge conventions in @.claude/rules/git.md.
- LLM responses are cached in Redis — changing prompt templates or model config needs cache invalidation.
- Every LLM/embedding call gets the Langfuse callback handler — missing traces usually means it wasn't attached.
- API testing: Bruno collection at `kebi-config/bruno/` — new endpoints get a corresponding `.bru` file there.
- DB write ownership is split with the product repo — table map in @.claude/rules/architecture.md; never cross it.
