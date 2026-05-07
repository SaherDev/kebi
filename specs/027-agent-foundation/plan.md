# Implementation Plan: Agent Foundation (M0.5 + M1 + M2 + M3)

**Branch**: `027-agent-foundation` | **Date**: 2026-04-21 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/Users/saher/dev/repos/kebi-dev/kebi/specs/027-agent-foundation/spec.md`
**Source of truth**: `docs/plans/2026-04-21-agent-tool-migration.md` (milestones M0.5 / M1 / M2 / M3). Binding ADRs: ADR-044, ADR-048, ADR-051, ADR-052, ADR-057, ADR-059, ADR-062, plus new ADR-063 landing in this feature.

## Summary

Land the externally-visible contract cleanup (ExtractPlaceResponse two-level status with verbatim `raw_input`) and all the internal scaffolding the LangGraph agent needs to run — without wiring the agent to `/v1/chat` yet. Four concrete deliverables:

1. **M0.5** — Rewrite `ExtractPlaceResponse` / `ExtractPlaceItem` to separate pipeline-level status (`pending`/`completed`/`failed`) from per-place status (`saved`/`needs_review`/`duplicate`). Rename `source_url → raw_input` (verbatim echo). Bump Redis key prefix to `extraction:v2:`. Coordinate with the product repo.
2. **M1** — Inline-await the extraction pipeline inside `ExtractionService.run()`. Move the `asyncio.create_task` fire-and-return to `ChatService._dispatch_extraction` so HTTP callers still see `pending` immediately.
3. **M2** — Add `agent:` config block (`enabled=false`, per-tool timeouts, step/error ceilings), ship `config/prompts/agent.txt` with the places-advisor persona, register it through `PromptConfig`, and add a typed `AgentConfig`/`ToolTimeoutsConfig` on `AppConfig`.
4. **M3** — Greenfield `core/agent/` module: `AgentState` TypedDict, `build_turn_payload` helper (transient-field reset), `ReasoningStep` Pydantic model, `build_graph` factory with `agent`/`tools`/`fallback` nodes + `should_continue` router, `build_checkpointer` backed by `AsyncPostgresSaver`, Alembic exclusion filter for checkpointer tables.

Per clarifications: prompt + config validated eagerly at boot; checkpointer + LLM-bound graph lazy (first flag-on `/v1/chat` call) and cached; flag evaluated per-request so M10 can flip in-prod. `agent_node` takes injected LLM — M3 tests only mock. Fallback emits one user-visible `ReasoningStep` now; debug diagnostics deferred to M9. Legacy `extraction:v1:*` keys are unread after deploy (polling returns 404, same path as TTL expiry).

Agent wiring to `/v1/chat`, tool wrappers, streaming, `NodeInterrupt`, per-tool timeouts, flag flip, legacy deletion — all deferred to M4–M11 per the plan doc.

## Technical Context

**Language/Version**: Python 3.11 (`>=3.11,<3.14` per `pyproject.toml`; constitution ADR-006 bounds 3.11–3.12)
**Primary Dependencies**: FastAPI ^0.115, Pydantic ^2.10, pydantic-settings ^2.7, LangChain ^0.3, LangGraph ^0.3, langchain-anthropic ^0.3, SQLAlchemy ^2.0 async, asyncpg ^0.30, Alembic ^1.14, redis ^5.0, Instructor ^1.0, Langfuse ^3.0
**New dependency**: `langgraph-checkpoint-postgres` (pin at install; verify latest on PyPI — plan references `^2.0` as a placeholder, FR-029 makes the final pin a verified install-time decision)
**Storage**: PostgreSQL via asyncpg (Railway; local via docker-compose). Library-managed checkpointer tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`) live in the same DB. Redis for extraction status (`extraction:v2:*` after this feature).
**Testing**: pytest ^8.3 with `asyncio_mode = "auto"`. `InMemorySaver` from `langgraph-checkpoint` for agent-graph unit tests (no Postgres round-trip). Integration-style test for `setup()` idempotency runs against docker-compose Postgres.
**Target Platform**: Linux server (Railway). Local dev via docker-compose.
**Project Type**: Web service (FastAPI). Single-project repo per ADR-001 (src layout).
**Performance Goals**: No new latency budget in this feature (agent not wired). Baseline preserved: `ExtractionService.run()`'s inline await replaces an existing background task of equivalent latency; the create_task moves up to the route layer. Polling route latency unchanged.
**Constraints**: `agent.enabled=false` by default — no user-visible behavior change aside from the `ExtractPlaceResponse` shape. Startup must NOT require `DATABASE_URL` to be reachable or `ANTHROPIC_API_KEY` to be present (per clarification). Prompt file must be loaded + validated eagerly so malformed prompts fail loud at boot.
**Scale/Scope**: Four milestone slices. Estimated surface: ~1 schema file rewritten (`extract_place.py`), ~1 service inlined (`extraction/service.py`), ~1 route helper reshaped (`chat/service.py::_dispatch_extraction`), ~1 route unchanged (`api/routes/extraction.py` — returns new shape naturally via Pydantic), ~1 status repo constant changed (`extraction/status_repository.py`), ~1 new config block + `AgentConfig`/`ToolTimeoutsConfig` types, ~1 new prompt file, ~5–7 new files under `core/agent/`, ~1 Alembic env edit, ~1 ADR-063 entry, docs+Bruno updates. Estimated new LOC: 600–900 including tests.

All Technical Context entries resolved — no NEEDS CLARIFICATION markers.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Evaluated against `.specify/memory/constitution.md` (v1.0, 2026-03-08) plus the binding ADRs not yet migrated into the constitution file (ADRs 028–062). The constitution file lists ADRs up to ADR-044; newer ADRs (-048, -051, -052, -054–-061, -062) are binding per CLAUDE.md and the spec's header note. I evaluate both.

| Principle | Verdict | Notes |
|---|---|---|
| I. Repo Boundary (NON-NEGOTIABLE) | **PASS** | Changes only AI-side schemas, services, config, and a new `core/agent/` module. No UI / auth / product-CRUD. No imports from the product repo. |
| II. Architecture Decisions are Constraints | **PASS with new ADR** | Adds ADR-063 (two-level `ExtractPlaceResponse` status + `raw_input` rename) per FR-007. No existing ADR contradicted. ADR-062 is the binding agent-architecture decision; this feature lands M0.5–M3 of ADR-062's rollout. |
| III. Provider Abstraction (NON-NEGOTIABLE) | **PASS** | `agent_node` takes an injected LLM; real binding happens in M6 via `get_llm("orchestrator")`. No hardcoded model names anywhere in this feature. M2's `agent:` config block does not duplicate model-role mapping. |
| IV. Pydantic Everywhere | **PASS** | `ExtractPlaceResponse`, `ExtractPlaceItem`, `AgentConfig`, `ToolTimeoutsConfig`, `ReasoningStep` all Pydantic. `AgentState` is a `TypedDict` (LangGraph requirement — not a module/function boundary, internal to the graph), which is standard practice and consistent with ADR-062. |
| V. Configuration Rules | **PASS** | `agent:` block in `config/app.yaml` (committed, non-secret). Prompt file in `config/prompts/agent.txt` (committed). No secrets added. `get_config()` / `get_secrets()` remain the only access points. |
| VI. Database Write Ownership | **PASS** | Checkpointer writes to Postgres tables `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` — library-owned. Excluded from Alembic autogenerate (FR-031). No product tables touched. No embedding-dimension drift. |
| VII. Redis Ownership | **PASS** | Redis prefix bump `extraction:` → `extraction:v2:`. Still the only Redis owner. Checkpointer intentionally does NOT use Redis (plan decision — Railway's default Redis lacks RedisJSON + RediSearch modules, and keeping vanilla Redis for the PlaceObject cache layer is a load-bearing simplification). |
| VIII. API Contract | **PASS** | `POST /v1/chat`, `GET /v1/extraction/{request_id}` unchanged at the route level; response envelope shape changes per ADR-063 (coordinated with product repo — FR-036). No new routes. |
| IX. Testing | **PASS** | pytest coverage matches `src/` layout. `mypy --strict` must pass. `ruff check` must pass. New modules get test files (FR-032 mandates `InMemorySaver` for graph tests). |
| X. Git & Commits | **PASS** | Commits on branch `027-agent-foundation` follow `type(scope): description` with `#TASK_ID` trailer. Feature branch from `dev`, merge to `dev`. Bruno `.bru` updates for the new response shape per FR-008. |
| ADR-044 (Prompt-injection mitigation) | **PASS** | `config/prompts/agent.txt` (M2) includes the three mitigations: defensive-instruction clause, XML `<context>` tag discipline for retrieved content, and a sentence binding tools to Instructor validation (enforced in M5's tool wrappers; referenced here). |
| ADR-048 (extraction polling route) | **PASS** | `GET /v1/extraction/{request_id}` preserved. New envelope shape flows through Pydantic naturally. 404 on legacy `v1:` prefix — same code path as TTL expiry (per clarification). |
| ADR-051 (secrets in `.env`) | **PASS** | No new secrets. `DATABASE_URL` already present. `ANTHROPIC_API_KEY` not required at boot per clarification. |
| ADR-052 (`/v1/chat` unified entry) | **PASS** | `/v1/chat` remains the only conversational route. This feature does not add new routes. |
| ADR-057 (confidence bands) | **PASS** | `ExtractPlaceItem.status ∈ {saved, needs_review, duplicate}` matches the two-band save gate. Below-threshold outcomes collapse into envelope-level `failed`. |
| ADR-059 (`config/prompts/`) | **PASS** | Uses existing `PromptConfig` loader. Registration pattern matches `taste_regen: taste_regen.txt`. |
| ADR-062 (LangGraph StateGraph for agent) | **PASS** | Implements the M0.5–M3 slice of ADR-062's rollout. `StateGraph` directly (no `create_react_agent`). `AgentState`, `should_continue`, `ToolNode` structure per ADR-062 requirements. |

**Gate verdict**: PASS. No violations. ADR-063 is a new ADR entry required by FR-007 and tracked as part of M0.5 deliverables — not a constitution violation.

**Complexity Tracking**: Empty (no deviations to justify).

## Project Structure

### Documentation (this feature)

```text
specs/027-agent-foundation/
├── plan.md              # This file (/speckit.plan output)
├── spec.md              # Feature specification (already written)
├── research.md          # Phase 0 output — dependency + approach research
├── data-model.md        # Phase 1 output — Pydantic / TypedDict shapes
├── quickstart.md        # Phase 1 output — local verification walkthrough
├── contracts/           # Phase 1 output — API + config contracts
│   ├── extract_place.openapi.yaml       # v2 ExtractPlaceResponse envelope
│   ├── chat_extract_dispatch.md         # internal contract for /v1/chat extract-place path
│   ├── agent_config.schema.yaml         # config/app.yaml agent: block schema
│   └── agent_prompt.template.md         # agent.txt template-slot contract
├── checklists/
│   └── requirements.md  # Already written by /speckit.specify
└── tasks.md             # Written by /speckit.tasks (NOT this command)
```

### Source Code (repository root)

The repo is a single-project Python service (src layout, ADR-001). This feature touches four areas:

```text
src/kebi/
├── api/
│   ├── routes/
│   │   └── extraction.py                # UNCHANGED (returns new ExtractPlaceResponse shape via Pydantic)
│   └── schemas/
│       └── extract_place.py             # REWRITE — two-level status + raw_input rename (M0.5)
├── core/
│   ├── agent/                           # NEW — greenfield module (M3)
│   │   ├── __init__.py
│   │   ├── state.py                     # AgentState TypedDict
│   │   ├── invocation.py                # build_turn_payload helper
│   │   ├── reasoning.py                 # ReasoningStep Pydantic model
│   │   ├── graph.py                     # build_graph + should_continue + fallback_node + agent_node
│   │   └── checkpointer.py              # build_checkpointer coroutine
│   ├── chat/
│   │   └── service.py                   # EDIT — _dispatch_extraction wraps run() in asyncio.create_task (M1)
│   ├── config.py                        # EDIT — add AgentConfig + ToolTimeoutsConfig (M2)
│   ├── consult/
│   │   └── (api/schemas/consult.py)     # EDIT — ReasoningStep re-export (M3, FR-024)
│   └── extraction/
│       ├── service.py                   # EDIT — inline await, remove create_task (M1)
│       ├── persistence.py               # LIGHT EDIT — helper rename only if needed (M1)
│       └── status_repository.py         # EDIT — prefix bump "extraction" → "extraction:v2" (M0.5)
│
config/
├── app.yaml                             # EDIT — add agent: block + prompts: agent entry (M2)
└── prompts/
    ├── agent.txt                        # NEW — places-advisor persona + ADR-044 mitigations (M2)
    └── taste_regen.txt                  # UNCHANGED
│
alembic/
└── env.py                               # EDIT — include_object filter for checkpointer tables (M3, FR-031)
│
pyproject.toml                           # EDIT — add langgraph-checkpoint-postgres dep (M3, FR-029)
│
docs/
├── api-contract.md                      # EDIT — new ExtractPlaceResponse shape (M0.5, FR-007)
└── decisions.md                         # EDIT — add ADR-063 entry (M0.5, FR-007)
│
kebi-config/bruno/                     # EDIT (external repo path) — update .bru example responses (M0.5, FR-008)

tests/
├── api/
│   ├── routes/
│   │   └── test_extraction.py           # EDIT — polling returns new shape
│   └── schemas/
│       └── test_extract_place.py        # REWRITE — v2 envelope coverage
├── core/
│   ├── agent/                           # NEW
│   │   ├── conftest.py                  # checkpointer/InMemorySaver fixture
│   │   ├── test_state.py                # AgentState + add_messages reducer
│   │   ├── test_reasoning.py            # ReasoningStep defaults + visibility Literal
│   │   ├── test_invocation.py           # build_turn_payload resets transient fields
│   │   ├── test_graph_routing.py        # should_continue branches (unit, no LLM)
│   │   ├── test_fallback.py             # fallback_node emits user-visible ReasoningStep + message
│   │   ├── test_agent_node.py           # mocked LLM — prompt render + steps_taken + append
│   │   └── test_checkpointer.py         # setup() idempotency (integration: docker-compose Postgres)
│   ├── chat/
│   │   └── test_service.py              # EDIT — _dispatch_extraction route-layer create_task
│   ├── extraction/
│   │   └── test_service.py              # REWRITE — run() inline await; delete test_run_fires_background_task
│   └── config/                          # NEW (if missing) or EDIT
│       └── test_config.py               # typed AgentConfig roundtrip + defaults
```

**Structure Decision**: Single-project src layout per ADR-001. `core/agent/` is a new sibling to existing `core/{chat,consult,extraction,recall,...}` modules, mirroring the established pattern. No monorepo / backend-frontend split. Test layout mirrors src per ADR-004.

## Complexity Tracking

*Empty — Constitution Check passed with no violations to justify.*

## Phase 0 — Outline & Research

Output: [research.md](./research.md). Research decisions:

1. **`langgraph-checkpoint-postgres` version pin**. Plan references `^2.0` as a placeholder. Verify current latest on PyPI at install time; commit exact minor pin to `pyproject.toml`. Document the verified version in research.md. Confirm Python 3.11 compatibility matrix with LangGraph ^0.3.
2. **`AsyncPostgresSaver.from_conn_string` vs pool-based construction**. Two APIs exist. `from_conn_string` opens a connection string internally; `AsyncConnectionPool`-based construction shares a pool with the app. Plan chooses `from_conn_string` for simplicity; confirm this matches 2.x API and that it's safe to call `setup()` multiple times (FR-030). Document any pool-vs-connection-string tradeoffs for M6 to revisit.
3. **Prompt-validation strictness**. Per clarification, both `{taste_profile_summary}` and `{memory_summary}` slots must be present at boot. Research the cleanest placement: (a) extend `_load_prompts()` in `config.py` with per-prompt slot validation, (b) add a dedicated `agent_prompt_validator` invoked from the agent module, or (c) rely on `str.format_map(SafeDict)` at substitution time. Decide on (a) — keeps validation at the eager-boot layer (FR-018a).
4. **Redis prefix migration strategy**. Plan decides on `extraction:v2:{request_id}`. Confirm that writing only under `v2:` and reading only under `v2:` is sufficient (no compatibility read path per clarification). Research whether the `ExtractionStatusRepository` prefix constant should be configurable (via `config/app.yaml`) or a module-level constant. Decide: module-level constant with a single call site — simpler, no runtime toggle.
5. **Per-turn reset semantics for LangGraph**. Validate the claim in FR-022 that `{"last_recall_results": None, "reasoning_steps": []}` in the invocation payload overwrites (rather than merges) when the field has no reducer. Confirm in LangGraph 0.3 docs / source. Document the minimal invocation-time state shape that guarantees reset behavior, and how `add_messages` reducer behavior for `messages` coexists with plain-overwrite for the transient fields.
6. **Fallback-node ReasoningStep emission shape**. Per clarification, M3 emits one user-visible step. Research whether LangGraph's conditional-edge pattern lets the fallback node both compose an `AIMessage` and append to `reasoning_steps` in one state update. Document the exact `Command(update=...)` shape.
7. **Alembic `include_object` filter**. Confirm the 1.14 API accepts `include_object=<callable>` at `context.configure(...)` and that it's sufficient to filter both `run_migrations_online` and `run_migrations_offline`. Document the hook that keeps the three library-owned tables invisible to autogenerate.
8. **Product-repo coordination protocol**. Document the merge order for FR-036: AI repo ADR-063 + schema + prefix bump land on `dev` → product repo ships matching TypeScript schema on its `dev` → then AI repo's deploy. Note the Bruno update (FR-008) as the live reference point.

**Output**: `research.md` with one Decision/Rationale/Alternatives entry per item above. All NEEDS CLARIFICATION resolved (none present in Technical Context).

## Phase 1 — Design & Contracts

Prerequisites: `research.md` complete. Outputs below.

### 1. Data model — `data-model.md`

Entities, fields, constraints, state transitions. Covers:

- **ExtractPlaceResponse** — pipeline envelope. Fields: `status: Literal["pending", "completed", "failed"]`, `results: list[ExtractPlaceItem]`, `raw_input: str | None`, `request_id: str | None`. Invariant: `results` empty iff `status != "completed"`. Validators enforce.
- **ExtractPlaceItem** — per-place outcome. Fields: `place: PlaceObject` (required), `confidence: float` (required, `0.0–1.0`), `status: Literal["saved", "needs_review", "duplicate"]`. No null placeholders.
- **AgentConfig** — `enabled: bool`, `max_steps: int`, `max_errors: int`, `checkpointer_ttl_seconds: int`, `tool_timeouts_seconds: ToolTimeoutsConfig`. Nested under `AppConfig`.
- **ToolTimeoutsConfig** — `recall: int`, `consult: int`, `save: int`. Defaults 5/10/25. Pydantic validators ensure positive values.
- **AgentState** (TypedDict, LangGraph constraint) — `messages: Annotated[list[BaseMessage], add_messages]`, `taste_profile_summary: str`, `memory_summary: str`, `user_id: str`, `location: dict | None`, `last_recall_results: list[PlaceObject] | None`, `reasoning_steps: list[ReasoningStep]`, `steps_taken: int`, `error_count: int`. Transitions: `build_turn_payload` resets `last_recall_results=None`, `reasoning_steps=[]`, `steps_taken=0`, `error_count=0` on each user turn; `messages` appends via reducer.
- **ReasoningStep** — `step: str`, `summary: str`, `source: Literal["tool", "agent", "fallback"]`, `tool_name: Literal["recall", "save", "consult"] | None`, `visibility: Literal["user", "debug"]`, `timestamp: datetime` (default `datetime.now(UTC)`). `tool_name` MUST be set on `tool.summary` steps and MUST be `None` on `fallback` and on `agent.tool_decision` where no tool was decided.
- **AgentPrompt** (operational artifact, not a Pydantic model) — text file at `config/prompts/agent.txt`. Template slots: `{taste_profile_summary}`, `{memory_summary}`. Loaded and validated eagerly at `get_config()` time (FR-018a). Missing slot → loud boot failure.
- **Checkpointer tables** (library-owned, NOT in Alembic) — `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`. Schema managed by `langgraph-checkpoint-postgres`. Excluded via `include_object` filter.

### 2. Contracts — `contracts/`

Four contract artifacts:

**`contracts/extract_place.openapi.yaml`** — OpenAPI fragment for the v2 `ExtractPlaceResponse` returned by both `POST /v1/chat` (extract-place dispatch) and `GET /v1/extraction/{request_id}`. Includes `status` enum, `results[].{place, confidence, status}` with non-nullable fields, `raw_input` as the renamed field. Includes the `extraction:v2:*` Redis key convention as an `x-internal-notes` extension for documentation only.

**`contracts/chat_extract_dispatch.md`** — Internal contract for `ChatService._dispatch_extraction`: HTTP path must return `ChatResponse(type="extract-place", message="On it …", data=<ExtractPlaceResponse with status="pending">)` synchronously, schedule the inline-await pipeline via `asyncio.create_task`, and rely on `ExtractionService` to write the final envelope under `extraction:v2:{request_id}`. No internal-facing envelope leaks to callers.

**`contracts/agent_config.schema.yaml`** — YAML shape contract for the `agent:` block in `config/app.yaml`. Required keys: `enabled`, `max_steps`, `max_errors`, `checkpointer_ttl_seconds`, `tool_timeouts_seconds.{recall,consult,save}`. Defaults documented. Any missing key is a boot-time config failure (Pydantic validation).

**`contracts/agent_prompt.template.md`** — Template-slot contract for `config/prompts/agent.txt`. Mandatory slots: `{taste_profile_summary}`, `{memory_summary}`. Forbidden content: per-tool arg-shaping rules (those live on `@tool` docstrings in M5). Required sections: persona (places advisor — full `PlaceType` range), high-level tool-use guidance (when-to-call-recall / save / consult, no args), safety block (ADR-044 mitigations). Slot-presence check performed in `_load_prompts()`.

### 3. Quickstart — `quickstart.md`

Local verification walkthrough:
1. Checkout the branch, `poetry install` (pulls `langgraph-checkpoint-postgres`).
2. `docker compose up -d` — start Postgres + Redis.
3. `poetry run pytest tests/api/schemas/test_extract_place.py` — verify v2 envelope.
4. `poetry run pytest tests/core/extraction/test_service.py` — verify inline-await behavior.
5. `poetry run pytest tests/core/agent/` — verify graph skeleton with `InMemorySaver` + mocked LLM.
6. `poetry run pytest tests/core/agent/test_checkpointer.py` — integration: setup() idempotency against docker-compose Postgres.
7. `poetry run python -c "from kebi.core.config import get_config; c = get_config(); print(c.agent.enabled, c.prompts['agent'].file)"` → prints `False agent.txt`.
8. `poetry run alembic check` — confirm checkpointer tables not flagged.
9. Start uvicorn: `poetry run uvicorn kebi.api.main:app --reload`. `POST /v1/chat` with `{"user_id": "u1", "message": "https://tiktok.com/@x/video/123"}` → `data.status="pending"`, `data.raw_input="https://tiktok.com/@x/video/123"`, `data.request_id=<uuid>`. `GET /v1/extraction/<uuid>` → eventually `data.status="completed"` with real `results`.
10. `poetry run ruff check src/ tests/ && poetry run ruff format --check src/ tests/ && poetry run mypy src/` — all green.

### 4. Agent context update

Run `.specify/scripts/bash/update-agent-context.sh claude` after Phase 1 artifacts are on disk. This updates `CLAUDE.md`'s Recent Changes + Active Technologies bullets to include:
- `langgraph-checkpoint-postgres` as a new dependency
- `config.agent.*` (flag, timeouts, ceilings) as new config surface
- `core/agent/` module as a new code area
- `ExtractPlaceResponse` v2 schema + `raw_input` rename + `extraction:v2:` Redis prefix
- ADR-063 reference

Preserves manual additions between markers per the script's contract.

### Post-Phase-1 Constitution Re-check

After Phase 1 artifacts are written, re-evaluate the same constitution table. Expected outcome: still PASS — Phase 1 adds no new architectural surface beyond what Phase 0 research already covered. Document the re-check outcome at the bottom of `research.md`.

## Out-of-Band Deliverables (not Phase 0/1 artifacts, but required by the feature)

These land in normal source code during `/speckit.implement`, not as planning artifacts:

- **ADR-063** written into `docs/decisions.md` (short entry: context + decision + consequences covering two-level status + `raw_input` rename + Redis prefix bump).
- **`docs/api-contract.md`** updated — move `status` to envelope, drop nullability on `place`/`confidence`, rename `source_url → raw_input`.
- **Bruno collection** (`kebi-config/bruno/`) updated — example responses reflect v2 shape.
- **Product-repo coordination** — FR-036. Merge order documented in `research.md` item 8.

## Stop & Report

This command stops here. `tasks.md` is produced by `/speckit.tasks`.
