# Quickstart: Agent Foundation (M0.5 + M1 + M2 + M3)

**Feature branch**: `027-agent-foundation`
**Purpose**: Local-dev verification of every acceptance criterion in `spec.md` that can be exercised without the product repo deployed.

## Prerequisites

- Checked out on branch `027-agent-foundation`.
- `.env` symlink present (points at `kebi-config/secrets/ai.env.local`). `DATABASE_URL` populated.
- `poetry install` has been run (should pull `langgraph-checkpoint-postgres`).
- Docker running.

## 1. Bring up local services

```bash
docker compose up -d
```

Starts Postgres (port 5432) and Redis (port 6379). Postgres is the backing store for the checkpointer; Redis holds extraction status.

## 2. Apply Alembic migrations (unchanged — this feature adds no migrations)

```bash
poetry run alembic upgrade head
```

## 3. M0.5 schema + M1 inline-await — unit tests

```bash
poetry run pytest tests/api/schemas/test_extract_place.py -v
poetry run pytest tests/core/extraction/test_service.py -v
poetry run pytest tests/core/chat/test_service.py -v
```

Expected: all green. Key assertions exercised:
- `ExtractPlaceResponse.status` is envelope-level, `results[i].status` is per-place (SC-001, SC-002).
- `ExtractionService.run()` returns `status in {"completed", "failed"}` synchronously, never `pending` (SC-003).
- `ChatService._dispatch_extraction` returns `status="pending"` + `request_id` immediately, with `raw_input` byte-identical to input (SC-004).
- Old `_run_background` test is gone.

## 4. M2 config scaffolding — typed load

```bash
poetry run python -c "\
from kebi.core.config import get_config; \
c = get_config(); \
print('enabled:', c.agent.enabled); \
print('max_steps:', c.agent.max_steps); \
print('max_errors:', c.agent.max_errors); \
print('recall_to:', c.agent.tool_timeouts_seconds.recall); \
print('agent_prompt:', c.prompts['agent'].file); \
"
```

Expected output:
```
enabled: False
max_steps: 10
max_errors: 3
recall_to: 5
agent_prompt: agent.txt
```

Any missing key or malformed prompt file aborts with a clear `ValueError` / `FileNotFoundError` at `get_config()` — that's the eager-boot validation working.

## 5. M2 prompt content check

```bash
poetry run python -c "\
from kebi.core.config import get_config; \
content = get_config().prompts['agent'].content; \
assert '{taste_profile_summary}' in content, 'missing taste slot'; \
assert '{memory_summary}' in content, 'missing memory slot'; \
print('slots ok; length:', len(content), 'chars'); \
"
```

## 6. M3 agent graph skeleton — unit tests (mocked LLM, `InMemorySaver`)

```bash
poetry run pytest tests/core/agent/ -v -k "not checkpointer"
```

Expected: all green. Exercises:
- `AgentState` shape + `add_messages` reducer (SC-007, FR-020).
- `should_continue` branches: `tools`, `end`, `fallback via max_steps`, `fallback via max_errors` (SC-008).
- `build_turn_payload` resets `last_recall_results=None`, `reasoning_steps=[]` on every call (SC-009).
- `fallback_node` emits one user-visible `ReasoningStep` and composes a graceful `AIMessage` (FR-027).
- `agent_node` with a mocked LLM: binds tools, renders prompt with both slots substituted, increments `steps_taken`, appends response (FR-028).

## 7. M3 checkpointer — integration test (real Postgres)

```bash
poetry run pytest tests/core/agent/test_checkpointer.py -v
```

Expected: green. Assertions:
- First `await build_checkpointer()` creates tables `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`.
- Second call is idempotent — no errors, no schema drift (SC-010).

Verify tables exist:

```bash
docker compose exec postgres psql -U postgres -d kebi -c "\dt checkpoint*"
```

Expected output: three tables present.

## 8. Alembic exclusion filter

```bash
poetry run alembic check
```

Expected: no diffs flagged against `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` (SC-011). If you see `DROP TABLE checkpoints` or similar in the autogenerate preview, the `include_object` filter in `alembic/env.py` is broken — revisit R7 in research.md.

## 9. Flip the flag live (no code change)

Edit `config/app.yaml`:

```yaml
agent:
  enabled: true     # was false
```

Restart the service:

```bash
poetry run uvicorn kebi.api.main:app --reload
```

In another shell:

```bash
poetry run python -c "from kebi.core.config import get_config; print(get_config().agent.enabled)"
```

Expected: `True` (SC-005). Reverting the YAML and re-reading prints `False`. No code changed.

> NOTE: Flipping the flag in this feature has no user-visible effect — the agent path isn't wired to `/v1/chat` until M6. The flag is exercised here to prove the plumbing, not to test a behavior change.

## 10. End-to-end smoke: `/v1/chat` extract-place

```bash
curl -sS -X POST http://localhost:8000/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"u_demo","message":"https://www.tiktok.com/@example/video/123"}' \
  | tee /tmp/chat-resp.json
```

Expected shape (new envelope):

```json
{
  "type": "extract-place",
  "message": "On it — extracting the place in the background. Check back in a moment.",
  "data": {
    "status": "pending",
    "results": [],
    "raw_input": "https://www.tiktok.com/@example/video/123",
    "request_id": "<hex-uuid>"
  }
}
```

Wait a few seconds, then poll:

```bash
REQ_ID=$(jq -r '.data.request_id' /tmp/chat-resp.json)
curl -sS "http://localhost:8000/v1/extraction/$REQ_ID" | jq
```

Expected: `data.status="completed"` (or `"failed"` if the URL has no real place), `raw_input` still the original string, each `results[i]` carries a non-null `place`, `confidence`, and `status ∈ {saved, needs_review, duplicate}`.

## 11. Redis sanity check

```bash
docker compose exec redis redis-cli --scan --pattern "extraction:v2:*"
```

Expected: at least one key (`extraction:v2:<request_id>`). If you see `extraction:<request_id>` without the `v2:` segment, the prefix bump in `status_repository.py` did not land — revisit R4 in research.md.

## 12. Static checks

```bash
poetry run ruff check src/ tests/
poetry run ruff format --check src/ tests/
poetry run mypy src/
```

Expected: all green (SC-012). Zero new warnings introduced over the baseline.

## 13. Full test suite

```bash
poetry run pytest -x
```

Expected: all green (SC-013). With `agent.enabled=false`, the existing `/v1/chat` test suite passes unchanged.

---

## Exit criteria

All of 3 through 13 pass. If any fail:

- Schema failures (step 3) → check the `ExtractPlaceResponse` validator and `_outcome_to_item_dict` rename.
- Config failures (step 4/5) → check YAML block + `AgentConfig`/`ToolTimeoutsConfig` types + `_load_prompts` slot validation.
- Graph failures (step 6) → check `should_continue` branching + `build_turn_payload` reset fields + `fallback_node` emission shape.
- Checkpointer failures (step 7/8) → check `AsyncPostgresSaver.from_conn_string` use and Alembic `include_object` filter.
- Polling failures (step 10) → check `_status_repo.write` key format (must be `extraction:v2:<id>`) and the route's Pydantic deserialization of the new shape.
