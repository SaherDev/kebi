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
- Chat's render contract is text + `kebi://{venue|area}/{key}` links only (ADR-136) — links are attached server-side after the agent writes, so the agent must name places in plain prose, never markdown.
- `/v1/chat` needs `local_time` from the client for schedule-aware answers (ADR-138) — without it kebi won't assert what day it is, so "tonight is X's night" never fires.
- Place-tool filter vocabulary lives in the tool arg descriptions (`_search_args.py`), not the agent prompt (ADR-137) — don't move it back.
- `web_search` fires entirely on the agent's judgement (ADR-145) — no code gate, no entitlement gate, no "corpus first" rule; a shared Redis cache keyed on the question, not the user, is what makes that affordable.
- Web findings are mined into claims only when the harvest marks them durable (ADR-145) — event dates and prices are answered and deliberately never stored, so a `web_search` turn writing zero claims is correct, not a bug.
- Area icons come off the location resolver's output, not a separate call (ADR-146) — a venue's `icon` rides its catalog row, and both stay nullable so the client keeps its fallback.
- `movement_profile.source` distinguishes a chosen capability from a seeded guess (ADR-155) — absent means `default`, and an unchosen row's modes are ignored; modes stated mid-conversation are trip-scoped, outrank the settings row, and clear on a country change.
- Unknown movement guesses **wide**, not narrow (ADR-156) — the fallback leads with `rideshare` and an inferred narrow mode is lifted in code, because capping at walking hides places silently; a mode the user stated and a walkable-tier turn are never widened, and the agent owes the distance out loud.
- `/v1/chat`'s `user_profile` block is a prior, not an instruction (ADR-154) — the user's prose loses to observed behavior, except a restriction stated in it; kebi stores none of it, and entry/visa questions are searched live every turn, never banked as claims.
- Area URIs carry the geo key **encoded** (`kebi://area/{token}`, ADR-153) — mint/decode only via `core/areas/keys.py`; an area's row is created lazily by the profiler on first open of `GET /v1/areas/{id}`, and row presence *is* the "already profiled" signal.
- Geo identity is the registry, never a name (ADR-169) — keys are id-paths minted once per unique area via `core/geo/registry.py`; never hardcode a place-name mapping anywhere: a wrong area is fixed by editing its `geo_areas` row (colloquial/groups_into/splits) then running `scripts/rederive_geo_keys.py`; old slug tokens resolve via `legacy_key`.
- The location resolver can say a turn isn't about a place at all (`location_irrelevant`, ADR-145) — without it a world question resolves to nothing, reads as a clarification, and ends the turn asking which city they meant.
- The visible reasoning trace is model-authored (ADR-157) — every location frame is `debug`, the orchestrator's "thinking" step is the user-visible line (narration text on tool turns, never the answer text), and a step's visibility must never differ between its `active` and `done` frames or the client strands the skeleton.
- Agent tokens stream live (ADR-158/159) — narration types into the thinking row (`reasoning_delta`), answer prose promotes to `message_delta`, both plain prose; `kebi://` links only ever ride the terminal `message` frame, which the client swaps in wholesale. `DeltaBuffer` (`core/chat/delta_buffer.py`) owns the narration-vs-answer verdict; don't linkify or reshape delta text anywhere else.
- API testing: Bruno collection at `kebi-config/bruno/` — new endpoints get a corresponding `.bru` file there.
- DB write ownership is split with the product repo — table map in @.claude/rules/architecture.md; never cross it.
