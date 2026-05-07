# Quickstart — Agent Tools & Chat Wiring (028-agent-tools-wiring)

Local verification walkthrough for feature 028. Assumes feature 027 is merged on `dev` and its services are functional.

## Prerequisites

- Python 3.11 (matches `pyproject.toml` target).
- Poetry installed.
- Docker + `docker compose`.
- `.env` present in repo root (symlink → `kebi-config/secrets/ai.env.local`) with:
  - `DATABASE_URL`
  - `REDIS_URL`
  - `OPENAI_API_KEY` (for embeddings used by recall)
  - `ANTHROPIC_API_KEY` (for orchestrator / agent path)
  - `VOYAGE_API_KEY` (for embeddings — if configured)
  - `GOOGLE_API_KEY` (for Google Places discovery)
  - `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` (for tracing)

## Step 1 — Checkout & install

```bash
git checkout 028-agent-tools-wiring
poetry install
```

No new top-level dependencies in this feature — `poetry install` should be a no-op versus feature 027's lockfile.

## Step 2 — Start Postgres + Redis

```bash
docker compose up -d
```

Confirms the checkpointer tables from feature 027 are still present:

```bash
docker compose exec postgres psql -U postgres -d kebi -c "\dt"
# expect: checkpoints, checkpoint_blobs, checkpoint_writes listed
```

## Step 3 — Verify M4 refactor (ConsultService + PlaceFilters)

```bash
poetry run pytest -x \
    tests/core/places/test_filters.py \
    tests/core/recall \
    tests/core/consult \
    tests/db/repositories/test_recall_repository.py
```

**Expected**:
- `PlaceFilters` base + `RecallFilters` + `ConsultFilters` type round-trips work.
- `ConsultService.consult(...)` accepts pre-built `saved_places` + `ConsultFilters` + optional `emit` callback.
- `ConsultService.__init__` no longer requires `IntentParser` / `UserMemoryService`.
- `ConsultResponse.reasoning_steps` is gone — tests that previously asserted on that field now use a spy `emit` callback to capture the `(step, summary)` tuples and assert on them instead. Expected sequence per branch: geocode (when a location name resolves) → discover → merge → dedupe → enrich → tier_blend (warming) → chip_filter (active).
- `RecallService.run(..., emit=<spy>)` produces `("recall.mode", ...)` then `("recall.result", ...)` in order across filter-only and hybrid branches.
- `ExtractionService.run(raw, user_id, emit=<spy>)` produces `save.parse_input → save.enrich → (optional save.deep_enrichment) → save.validate → save.persist` in order; `save.deep_enrichment` fires only when Phase 3 (Whisper/vision) runs.
- Recall repository's WHERE-clause assembly walks nested attribute paths (unchanged from 027; formalized under the new base).

Also confirm the `mypy --strict` signature constraint:

```bash
poetry run mypy src/kebi/core/consult/service.py
# expect: 0 errors
```

## Step 4 — Verify M5 tool wrappers

```bash
poetry run pytest -x tests/core/agent/tools/
```

**Expected**:
- `RecallToolInput.model_json_schema()["properties"]` contains exactly `query`, `filters`, `sort_by`, `limit`. Asserted by `test_recall_tool.py`.
- `SaveToolInput.model_json_schema()["properties"]` contains exactly `raw_input`. Asserted by `test_save_tool.py`.
- `ConsultToolInput.model_json_schema()["properties"]` contains exactly `query`, `filters`, `preference_context`. Asserted by `test_consult_tool.py`.
- None of the three schemas contain `user_id`, `location`, or `saved_places` (SC-008).
- `_recall_summary` / `_save_summary` / `_consult_summary` return the expected narrations across outcome shapes. Asserted by `test_tool_summary_narration.py`.
- `build_emit_closure("recall")` returns `(collected, emit)` where `emit(step, summary, duration_ms=None)` appends a debug-visibility `ReasoningStep` to `collected` and fans out to the stream writer (when attached). `append_summary` appends the user-visible `tool.summary` with the same fan-out. Asserted by `test_emit_helpers.py` — both paths covered (writer attached, writer None).
- Every step in `collected` — debug and `tool.summary` alike — carries a non-null `duration_ms`. When `emit` is called with `duration_ms=None`, the closure computes it from the delta since the previous emit; when called with an explicit value, that value is preserved. The `tool.summary` step's `duration_ms` equals the total elapsed from the first emit to `append_summary`'s call time.
- For each tool, the returned `Command.update["reasoning_steps"]` ends with one `tool.summary` entry (`visibility="user"`), preceded by service-emitted debug entries in order.

## Step 5 — Verify M6 agent-graph integration (mocked LLM)

```bash
poetry run pytest -x \
    tests/core/agent/test_agent_graph_chain.py \
    tests/core/agent/test_recall_reset_between_turns.py \
    tests/core/agent/test_reasoning_visibility.py \
    tests/core/agent/test_reasoning_invariants.py \
    tests/core/agent/test_agent_decision_truncation.py \
    tests/core/agent/test_agent_decision_fallback.py
```

**Expected**:
- `test_agent_graph_chain.py`: With a scripted `GenericFakeChatModel` emitting (1) a recall tool call, then (2) a consult tool call, then (3) a final content-only response, the full turn succeeds. `state["last_recall_results"]` is written by recall and read by consult. The consult tool call's `args` in the captured `AIMessage.tool_calls[0]` does NOT contain `saved_places` (SC-009).
- `test_recall_reset_between_turns.py`: Two `graph.ainvoke` calls on the same `thread_id`. Turn 1 populates `last_recall_results` and `reasoning_steps`. At the start of Turn 2 (after `build_turn_payload` resets), both fields are back to `None` / `[]` while `messages` has accumulated.
- `test_reasoning_visibility.py`: A scripted recall→consult turn produces a full `reasoning_steps` list with debug + user entries. After filtering to `visibility="user"`, the JSON payload contains only the three user-visible types.
- `test_reasoning_invariants.py`: Parameterized over the 8 worked examples in the plan doc's M5 section. For each, assert the four catalog invariants: (1) every turn opens with one `agent.tool_decision`; (2) every tool call produces exactly one user-visible `tool.summary`; (3) `tool_name` set on `tool.summary`, `None` on `agent.tool_decision` and `fallback`; (4) direct-response turns have exactly one user-visible step.
- `test_agent_decision_truncation.py`: Scripted `AIMessage.content` of 500 chars → the `agent.tool_decision` state step's `summary` is ≤ 200 chars and ends cleanly.
- `test_agent_decision_fallback.py`: Scripted `AIMessage.content=""` with a recall tool call → summary falls back to the synthesized one-liner (`"recall — user referenced saved places"`).

## Step 6 — Verify M6 chat service wiring

```bash
poetry run pytest -x \
    tests/core/chat \
    tests/api/routes/test_chat.py \
    tests/api/schemas/test_chat.py
```

**Expected**:
- Flag-off `test_run_legacy_path`: classify_intent still runs; consult branch loads saved places inline and passes them to the new `ConsultService.consult` signature.
- Flag-on `test_run_agent_path`: `graph.ainvoke` is called once with the expected payload (`thread_id=user_id`, reset transient fields); response type is `"agent"`; `data.reasoning_steps` is populated with filtered user-visible steps.
- `ChatResponse.type` is now a `Literal`; constructing `ChatResponse(type="nonsense")` is rejected by Pydantic.

## Step 7 — Flag-off end-to-end smoke (no regression)

Default `config/app.yaml` has `agent.enabled: false`. Start the server:

```bash
poetry run uvicorn kebi.api.main:app --reload
```

Issue requests (in another shell):

```bash
curl -s -X POST http://127.0.0.1:8000/v1/chat \
     -H 'Content-Type: application/json' \
     -d '{"user_id":"u-flag-off","message":"show me my saved coffee shops"}' | jq .type
# expect: "recall"

curl -s -X POST http://127.0.0.1:8000/v1/chat \
     -H 'Content-Type: application/json' \
     -d '{"user_id":"u-flag-off","message":"where should I eat tonight?"}' | jq .type
# expect: "consult"

curl -s -X POST http://127.0.0.1:8000/v1/chat \
     -H 'Content-Type: application/json' \
     -d '{"user_id":"u-flag-off","message":"save https://tiktok.com/@foodie/video/123"}' | jq .type
# expect: "extract-place"
```

All three return the legacy types. No `"agent"` value.

## Step 8 — Flag-on end-to-end smoke

Edit `config/app.yaml`:

```yaml
agent:
  enabled: true       # was false
  max_steps: 10
  max_errors: 3
  checkpointer_ttl_seconds: 86400
  tool_timeouts_seconds:
    recall: 5
    consult: 10
    save: 25
```

Restart uvicorn (flag is read per-request, but the graph was built with flag-off semantics; restart ensures a clean lifespan construction regardless of how often this loop gets tested).

```bash
# Pure recall — "show me my saves"
curl -s -X POST http://127.0.0.1:8000/v1/chat \
     -H 'Content-Type: application/json' \
     -d '{"user_id":"u-agent-smoke","message":"show me my saved coffee shops"}' \
  | jq '{type, message, steps: .data.reasoning_steps | map(.step)}'
# expect: type="agent"; message lists coffee shops; steps = ["agent.tool_decision", "tool.summary"]

# Recommendation — recall then consult
curl -s -X POST http://127.0.0.1:8000/v1/chat \
     -H 'Content-Type: application/json' \
     -d '{"user_id":"u-agent-smoke","message":"find me a good ramen spot nearby","location":{"lat":35.66,"lng":139.70}}' \
  | jq '{type, steps: .data.reasoning_steps | map(.step)}'
# expect: type="agent"; steps ~ ["agent.tool_decision", "tool.summary", "agent.tool_decision", "tool.summary"]

# Save — URL
curl -s -X POST http://127.0.0.1:8000/v1/chat \
     -H 'Content-Type: application/json' \
     -d '{"user_id":"u-agent-smoke","message":"save https://tiktok.com/@foodie/video/123"}' \
  | jq '{type, steps: .data.reasoning_steps | map(.step)}'
# expect: type="agent"; steps = ["agent.tool_decision", "tool.summary"]

# Direct response — no tool call
curl -s -X POST http://127.0.0.1:8000/v1/chat \
     -H 'Content-Type: application/json' \
     -d '{"user_id":"u-agent-smoke","message":"is tipping expected in Japan?"}' \
  | jq '{type, steps: .data.reasoning_steps | map(.step)}'
# expect: type="agent"; steps = ["agent.tool_decision"] (single entry)
```

Verify multi-turn preservation:

```bash
# Turn 1 — recall
curl -s -X POST http://127.0.0.1:8000/v1/chat \
     -H 'Content-Type: application/json' \
     -d '{"user_id":"u-multi","message":"show me my saved places"}'

# Turn 2 — different user message, same user
curl -s -X POST http://127.0.0.1:8000/v1/chat \
     -H 'Content-Type: application/json' \
     -d '{"user_id":"u-multi","message":"what did I just ask about?"}' \
  | jq '{type, message}'
# expect: type="agent"; message references the prior turn via conversation history
```

## Step 8a — Verify the one-tool-call-per-response prompt instruction

Open `config/prompts/agent.txt`. Confirm the `## Your tools` section contains the one-tool-call instruction (e.g., "Emit at most one tool call per response. When a request needs more than one tool, chain sequentially across turns."). This is the primary mitigation for the parallel-tool-call race on `AgentState.reasoning_steps`.

After the flag-on smokes in step 8, inspect the Langfuse trace for the recommendation-request turn. For every `AIMessage` span, confirm `tool_calls` contains at most one entry. If Sonnet emits two calls in a single response under any of the five smoke prompts, the prompt instruction is being ignored — flag it before the flag flip in M10. The automated version of this check (`tests/core/agent/test_one_tool_call_per_response.py`) ships with M9 per the plan doc; until then, manual verification during canary is sufficient.

## Step 9 — Langfuse trace check

After Step 8, open the Langfuse UI (`$LANGFUSE_HOST`):

- Find the latest trace for `user_id=u-agent-smoke`.
- Recommendation-request trace should contain: 1 outer trace with `user_id=u-agent-smoke`, one LLM span per `agent_node` run, one tool span per tool call (recall + consult), one final LLM span for the composer reply.
- All spans should be linked under the same trace tree — no orphans.

Manual validation for SC-010 + FR-032.

## Step 10 — Final lint + type check

```bash
poetry run ruff check src/ tests/
poetry run ruff format --check src/ tests/
poetry run mypy src/
```

All three commands must exit 0.

## Step 11 — Flip the flag back to off before committing

```bash
# Revert the manual edit to config/app.yaml:
#   agent.enabled: false
```

Feature 028 ships with `agent.enabled: false`. Flipping the default is explicitly out of scope (deferred to M10 in a later feature).

## Step 12 — Bruno collection spot-check

Open the Bruno collection at `kebi-config/bruno/`. Confirm:
- Existing `.bru` files for flag-off response types still match.
- New `chat_agent_example.bru` request + example response for the agent-path shape is present.

## What is NOT covered in this quickstart

The following are out of scope for this feature — operators running this quickstart should see no activity related to them:

- SSE streaming endpoint (M7).
- Needs-review save clarification flow (M8).
- Per-tool wall-clock timeouts / failure-budget operationalization (M9).
- Flag-on default (M10).
- Legacy intent pipeline deletion (M11).
