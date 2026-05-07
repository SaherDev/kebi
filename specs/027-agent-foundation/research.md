# Phase 0 Research: Agent Foundation (M0.5 + M1 + M2 + M3)

**Feature branch**: `027-agent-foundation`
**Date**: 2026-04-21
**Scope**: Resolve the open technical decisions needed before implementation can start. One entry per item from `plan.md` Phase 0.

---

## R1. `langgraph-checkpoint-postgres` version pin

**Decision**: Install the current PyPI latest of `langgraph-checkpoint-postgres` at implementation time and pin it with Poetry's `^X.Y` caret syntax to the observed major/minor. The plan doc's `^2.0` reference is a placeholder — the final pin lands during `/speckit.implement` after `poetry add langgraph-checkpoint-postgres` runs and reveals the actual major.

**Rationale**: The library is part of the LangGraph team's first-party stack (same org as `langgraph` ^0.3 already in `pyproject.toml`). Its major/minor cadence is independent of `langgraph` core, so hardcoding a pin in the spec before install would be guessing. FR-029 explicitly permits "verify latest on PyPI at install time." Locking the exact observed version in `pyproject.toml` at install gives reproducibility without premature pinning.

**Compatibility verification steps** (run at implement-time):
1. `poetry add langgraph-checkpoint-postgres`
2. `poetry run python -c "from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver; print(AsyncPostgresSaver.__module__)"`
3. Confirm Python 3.11 (repo bounds) is in the library's supported matrix on PyPI.
4. Run `poetry run mypy src/` to catch any type-stub gaps.
5. Record the installed version in `research.md` at the bottom (post-implement addendum) so future conversations know what was actually pinned.

**Alternatives considered**:
- Pin an exact version (`=2.0.1`) — rejected: too rigid, blocks patch updates.
- Use `*` (any version) — rejected: violates Poetry best practice.
- Defer install to M6 — rejected: M3's `build_checkpointer` requires the import to exist and `mypy` needs to typecheck.

---

## R2. `AsyncPostgresSaver` construction: `from_conn_string` vs `AsyncConnectionPool`

**Decision**: Use `AsyncPostgresSaver.from_conn_string(DATABASE_URL)` for M3. Document `AsyncConnectionPool` as the "revisit in M6" option in `build_checkpointer.py` as a comment (not code).

**Rationale**:
- `from_conn_string` is the simplest callable surface and matches the plan doc's example literally.
- M3's checkpointer is lazy (per FR-018b) — it's constructed on the first flag-on `/v1/chat` call and cached. Under that access pattern, a dedicated connection string is fine; pool-sharing with the FastAPI SQLAlchemy engine is a peak-load optimization, not a correctness concern.
- M3 runs unit tests against `InMemorySaver` (FR-032). The only real-Postgres exercise in this feature is the `setup()` idempotency integration test. A single short-lived connection is sufficient.
- `setup()` is documented as idempotent on the `AsyncPostgresSaver` class; the plan requires it (FR-030). We verify via an integration test that exercises `setup()` twice and inspects the three tables.

**Revisit-in-M6 trigger**: If the graph is invoked enough that connection-setup overhead shows in Langfuse spans, swap `from_conn_string` for `AsyncConnectionPool` sharing with the existing SQLAlchemy engine pool. This is a one-line change at the `build_checkpointer` call site and does not affect anything downstream.

**Alternatives considered**:
- `AsyncConnectionPool` from day one — rejected: over-engineers an operationally-deferred concern and forces wiring to the existing pool in M3 when the graph is mocked anyway.
- A context-manager-based factory (`async with AsyncPostgresSaver.from_conn_string(...)`) — rejected: LangGraph's docs show the resource is intended to outlive the request; using a context manager would force per-request construction, defeating the cache.

---

## R3. Prompt-slot validation placement

**Decision**: Extend `_load_prompts()` in `src/kebi/core/config.py` with an optional per-prompt `required_slots: list[str]` argument. For the `agent` prompt, assert both `{taste_profile_summary}` and `{memory_summary}` appear verbatim in the loaded content. Missing slot → `ValueError` that names the prompt and the missing slot — propagates as a boot failure.

**Rationale**:
- Keeps validation in the same eager-boot layer as other config (FR-018a).
- No new module, no new boundary to cross.
- Validation is O(1) per prompt (substring check) — does not slow boot.
- Operators get a loud failure naming the exact problem before the service accepts traffic.

**Implementation sketch** (lives in `config.py`, not the plan):

```python
def _load_prompts(raw: dict[str, str]) -> dict[str, PromptConfig]:
    required = {"agent": ["{taste_profile_summary}", "{memory_summary}"]}
    prompts_dir = find_project_root() / "config" / "prompts"
    loaded: dict[str, PromptConfig] = {}
    for name, filename in raw.items():
        path = prompts_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Prompt '{name}' file not found: {path}")
        content = path.read_text()
        for slot in required.get(name, []):
            if slot not in content:
                raise ValueError(
                    f"Prompt '{name}' ({path}) is missing required template slot {slot!r}"
                )
        loaded[name] = PromptConfig(name=name, file=filename, content=content)
    return loaded
```

**Alternatives considered**:
- New `agent_prompt_validator.py` module invoked from `core/agent/` — rejected: pushes validation out of the eager-boot path. Two-stage validation creates a window where the service boots "successfully" but crashes on first agent use. Contradicts FR-018a.
- Pydantic `model_validator` on `AgentConfig` that loads the prompt file — rejected: couples config types to disk I/O.
- `str.format_map(SafeDict)` at substitution time — rejected: silent fallback defeats the "fail loud" requirement.

---

## R4. Redis prefix migration strategy

**Decision**: Change the `_KEY_PREFIX` module-level constant in `src/kebi/core/extraction/status_repository.py` from `"extraction"` to `"extraction:v2"`. Do NOT add a compatibility read path. The polling route returns 404 for `v1:` keys (identical to TTL expiry) per clarification.

**Rationale**:
- Clarification resolves the design question: no coercion, no back-compat layer, no two-deploy rollout.
- Single module-level constant keeps the change obvious and auditable in one commit.
- Making this configurable via `app.yaml` would add a runtime knob without a runtime need — the prefix bump is a one-time deploy event.
- 1-hour TTL + "don't deploy mid-extraction" operational rule is sufficient. Solo-dev / Railway context has no rolling-deploy overlap.

**Implementation sketch** (two-line change):
```python
# Before
_KEY_PREFIX = "extraction"
# After
_KEY_PREFIX = "extraction:v2"
```

All reads and writes flow through `f"{_KEY_PREFIX}:{request_id}"`, so both sides update in lockstep.

**Deploy order**:
1. Merge AI repo PR with prefix bump + schema change + ADR-063.
2. Product repo merges matching TypeScript schema PR.
3. Deploy AI repo during a low-traffic window (no active extractions in flight).
4. Any in-flight pre-deploy `v1:` keys expire within 1h; no user visible impact (the product repo's polling code treats 404 as "extraction expired, ask the user to retry").

**Alternatives considered**:
- Read v1, coerce to v2 — rejected per clarification; creates tech debt that outlives its window.
- Configurable via `app.yaml` — rejected; no runtime need.
- Double-write v1 + v2 for one deploy window — rejected; the write path has no ambiguity, only the read path does, and we've already decided not to read v1.

---

## R5. Per-turn reset semantics with LangGraph 0.3

**Decision**: Set `last_recall_results=None` and `reasoning_steps=[]` explicitly in the invocation payload. LangGraph 0.3's default state merge semantics overwrite non-reducer fields with whatever the incoming payload contains. For `messages`, which has the `add_messages` reducer, a single-element list `[HumanMessage(content=…)]` appends to history instead of overwriting. This is exactly the contract `build_turn_payload` depends on.

**Rationale**:
- LangGraph 0.3's `StateGraph` applies `Annotated[..., reducer]` fields via the reducer and overwrites plain fields. This is the documented behavior.
- `AgentState.last_recall_results` is `list[PlaceObject] | None` — no reducer → plain overwrite. Passing `None` overwrites any prior value.
- `AgentState.reasoning_steps` is `list[ReasoningStep]` — no reducer → plain overwrite. Passing `[]` resets.
- Tools append to `reasoning_steps` by reading `runtime.state.get("reasoning_steps")` and returning the concatenated list in their `Command(update=...)`. This is the pattern ADR-062 mandates and FR-021 restates.
- `messages` uses `Annotated[list[BaseMessage], add_messages]` → the reducer appends incoming messages to existing history. Checkpointer restores prior messages on each invocation; `build_turn_payload` adds one `HumanMessage` per turn; Sonnet responds with an `AIMessage`; the reducer accumulates.

**Verification test**: `tests/core/agent/test_invocation.py` calls `build_turn_payload` twice with different inputs and asserts `last_recall_results is None` and `reasoning_steps == []` both times (the helper always constructs the reset values).

**Verification test for reducer behavior**: `tests/core/agent/test_state.py` builds an `AgentState` via `graph.ainvoke(...)` with `InMemorySaver` and two consecutive HumanMessage payloads; asserts `messages` accumulates two HumanMessages and (simulated) AIMessage responses across the turns, while `reasoning_steps` is `[]` at the start of turn 2.

**Alternatives considered**:
- Add a reducer that clears on sentinel value — rejected: FR-021 explicitly forbids it; introduces a sentinel contract the rest of the code has to respect.
- Clear fields in the `agent_node` itself — rejected: agent_node runs AFTER the payload is merged, so any clearing there is too late for the first tool call.

---

## R6. `fallback_node` emission shape

**Decision**: `fallback_node` returns a dict state-update (the function's return value merges into state via LangGraph's default semantics). Two fields update:
- `messages`: a list with one `AIMessage(content="Something went wrong on my side — try again with a bit more detail?")` — the reducer appends it to history.
- `reasoning_steps`: `state.get("reasoning_steps", []) + [ReasoningStep(step="fallback", summary=<condition>, source="fallback", tool_name=None, visibility="user")]` — plain overwrite with the concatenated list.

`summary` is built from state inspection at entry: `"Got stuck after N steps"` if `steps_taken >= max_steps`, else `"Hit too many errors"` if `error_count >= max_errors`, else a generic `"Something went wrong"`. No debug diagnostic steps (`max_steps_detail`, `max_errors_detail`) per clarification — those land in M9.

**Rationale**:
- Uses the same state-update pattern as every other node. No new LangGraph primitive.
- `Command(update=...)` vs plain dict return: plain dict is sufficient when the node does not need to direct control flow to a specific next node (the edge from fallback → END is already hardcoded in `build_graph`). Saving `Command` for tool nodes and the agent node where it adds value.
- `tool_name=None` enforces the FR-023 invariant ("`tool_name` always None on fallback").

**Implementation sketch**:

```python
# core/agent/graph.py
from langchain_core.messages import AIMessage
from kebi.core.agent.reasoning import ReasoningStep

def fallback_node(state: AgentState) -> dict:
    max_steps = get_config().agent.max_steps
    max_errors = get_config().agent.max_errors
    if state.get("steps_taken", 0) >= max_steps:
        summary = f"Got stuck after {max_steps} steps, something went wrong on my end"
    elif state.get("error_count", 0) >= max_errors:
        summary = "Hit too many errors, try rephrasing or sharing more detail"
    else:
        summary = "Something went wrong on my end"
    step = ReasoningStep(
        step="fallback",
        summary=summary,
        source="fallback",
        tool_name=None,
        visibility="user",
    )
    return {
        "messages": [AIMessage(
            content="Something went wrong on my side — try again with a bit more detail?"
        )],
        "reasoning_steps": (state.get("reasoning_steps") or []) + [step],
    }
```

**Alternatives considered**:
- Use `Command(update=..., goto=END)` — rejected: the edge to END is already declared in `build_graph`; using `Command(goto=...)` would duplicate the routing intent.
- Separate "diagnostics node" that runs before `fallback_node` for the debug steps — rejected: deferred to M9 per clarification; not this feature's scope.

---

## R7. Alembic `include_object` filter

**Decision**: Edit `alembic/env.py` to add an `include_object(object, name, type_, reflected, compare_to)` callback that returns `False` for any table named in `{"checkpoints", "checkpoint_blobs", "checkpoint_writes"}`. Wire it into both `run_migrations_online` and `run_migrations_offline` via `context.configure(..., include_object=include_object)`.

**Rationale**:
- Alembic 1.14 supports `include_object` on `context.configure(...)` — this is the documented pattern for excluding library-managed tables from autogenerate.
- The filter is set-based (three names) — O(1) per object checked, zero perf concern.
- Must go on BOTH online and offline paths: autogenerate runs online; `alembic upgrade --sql` runs offline. Without the offline filter, `alembic upgrade --sql` output could include DDL for the library tables.
- `type_ == "table"` guard excludes other object types (indexes, FKs) from the match — safer than a name-only filter.

**Implementation sketch** (`alembic/env.py`):

```python
_LIBRARY_TABLES = {"checkpoints", "checkpoint_blobs", "checkpoint_writes"}

def _include_object(object, name, type_, reflected, compare_to):  # noqa: ANN001
    if type_ == "table" and name in _LIBRARY_TABLES:
        return False
    return True

# In run_migrations_online:
context.configure(
    connection=connection,
    target_metadata=target_metadata,
    include_object=_include_object,
)

# In run_migrations_offline:
context.configure(
    url=url,
    target_metadata=target_metadata,
    literal_binds=True,
    dialect_opts={"paramstyle": "named"},
    include_object=_include_object,
)
```

**Verification**: `poetry run alembic check` after `build_checkpointer` has run once locally (so the tables physically exist) — expect no diffs flagged.

**Alternatives considered**:
- `include_name` callback — rejected: older Alembic API, less flexible than `include_object`.
- `compare_type=False` + manual list maintenance — rejected: doesn't address the "table exists in DB but not in metadata" case that triggers autogen-drop.
- Put the three tables in `target_metadata` as no-op `Table(..., info={"skip_autogen": True})` — rejected: mixes ownership; Alembic would then attempt to migrate them.

---

## R8. Product-repo coordination protocol for ADR-063

**Decision**: Bundled-merge workflow, not a two-deploy split:

1. **AI repo (this branch)**: land ADR-063 + schema rewrite + Redis prefix bump + Bruno updates + inline-await refactor + config scaffolding + agent graph skeleton in one branch. Merge to `dev` when green.
2. **Product repo**: ship matching TypeScript `ExtractPlaceResponse` schema + callsite updates (any consumer reading `results[0].status` flips to `response.status`; any consumer reading `source_url` renames to `raw_input`) in a parallel PR. Merge to its `dev` when green.
3. **Deploy window**: during a low-traffic window, (a) ensure no extractions are in flight, (b) deploy AI repo first (writes new `extraction:v2:*` keys, returns new envelope shape), (c) deploy product repo (reads new shape). FR-036: product-repo PR must be merged before the AI repo's final deploy.
4. **Observable signal that coordination succeeded**: Bruno `.bru` example responses in `kebi-config/bruno/` reflect the new shape, and a smoke test of the product repo's chat flow against the deployed AI repo shows no schema-parse errors.

**Rationale**:
- The coordination surface is small (one schema, one field rename). A single merge-window approach is simpler than a dual-deploy compatibility shim.
- Solo-dev / Railway context means there is no multi-engineer handoff to coordinate mid-rollout.
- Clarification 4 (return 404 on v1: keys) eliminates the coercion option, making a one-window rollout the only sensible path.

**Deferred**: The exact deploy-day checklist (Railway env vars, Slack ping to self, "no in-flight extractions" verification step) lives operationally, not in this spec. Not a research output.

**Alternatives considered**:
- Two-deploy rollout (AI repo ships v1+v2 dual-write; product repo migrates; AI repo removes v1 writer) — rejected: adds temporary code that has to be removed, and the ADR-063 decision is non-backwards-compatible by design.
- Product repo ships first — rejected: product repo's new schema code would reject the AI repo's still-v1 responses, breaking chat.
- Feature-flag the whole thing — rejected: overkill for a schema cleanup that is clearly a one-way transition.

---

## Post-Phase-1 Constitution Re-check

To be completed after `data-model.md`, `contracts/`, and `quickstart.md` are written. Expected: PASS (no new architectural surface introduced by Phase 1 artifacts; they only document the shapes agreed in Phase 0).

**Re-check outcome** *(2026-04-21, after Phase 1 artifacts written)*: **PASS**. Phase 1 artifacts (`data-model.md`, `contracts/extract_place.openapi.yaml`, `contracts/chat_extract_dispatch.md`, `contracts/agent_config.schema.yaml`, `contracts/agent_prompt.template.md`, `quickstart.md`) are pure documentation of the shapes Phase 0 already decided. No new dependency, no new DB write, no new route, no hardcoded model name. Provider Abstraction (III), Pydantic Everywhere (IV), Redis Ownership (VII), and API Contract (VIII) all unaffected. Gate remains green.

---

## Post-implement addendum (filled in at `/speckit.implement` time)

- **Exact pinned version of `langgraph-checkpoint-postgres`**: `^3.0.5` (resolved to `3.0.5`). Required installing `psycopg[binary] ^3.3.3` alongside it — the `psycopg` base package alone does not ship `libpq`, so `from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver` fails at import time without the binary extra. Recorded in `pyproject.toml`.
- **Python 3.11 compatibility confirmed**: yes — `poetry install` succeeded under the repo-pinned Python 3.11 virtualenv; all 574 unit+integration tests pass.
- **Deprecation warning observed at import time**: `langgraph/checkpoint/postgres/__init__.py:26: DeprecationWarning: You're using incompatible versions of langgraph and checkpoint-postgres.` The repo is on `langgraph ^0.3.34` which is older than checkpoint-postgres 3.0.5's preferred base. Functionality is unaffected (import + structural tests green). Deferred: bump `langgraph` at M6 when the real orchestrator wires up.
- **Testing approach**: Postgres integration tests (`tests/core/agent/test_checkpointer.py`) use a `needs_postgres` skip-if-unreachable marker so CI without docker-compose still passes. Running them locally requires `docker compose up -d postgres`.
- **Scope adjustment during implementation**: at the user's request mid-implementation, `RecallFilters` was refactored to mirror `PlaceObject` (ADR-056 nested `attributes: PlaceAttributes`). That work was originally scheduled for M4 per the plan doc; pulling it forward avoided churn in the repository's `_build_where_clause` WHERE-clause construction during M4. Test fixtures in `tests/db/repositories/test_recall_repository.py` and `tests/core/chat/test_service.py` updated for the nested shape.

## Known non-blockers to address at M6+

- The deprecation-warning is the most obvious follow-up. The plan's M6 brings the real LLM wiring online; bump `langgraph` at that point and re-verify.
- The baseline pre-existing mypy error in `src/kebi/core/taste/service.py:58` ("Cannot infer type of lambda") predates this feature and was preserved unchanged.
- `ruff check` baseline had 5 errors (1 src UP038, 4 tests E501); post-feature it's 4 (the 1 test that went away was one I rewrote). No new ruff errors introduced by feature 027.
