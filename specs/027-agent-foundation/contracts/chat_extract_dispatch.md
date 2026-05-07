# Internal contract: `ChatService._dispatch_extraction`

**Scope**: M1 refactor. Internal-only; no product-repo coordination.
**Location**: `src/kebi/core/chat/service.py`

## Invariants

### HTTP-facing behavior (UNCHANGED externally)

`POST /v1/chat` with an extract-place-intent message:

1. Returns **synchronously** within the intent-classification + dispatch budget.
2. Body: `ChatResponse(type="extract-place", message="On it — extracting the place in the background. Check back in a moment.", data=<ExtractPlaceResponse with status="pending">)`.
3. `data.status == "pending"`, `data.results == []`, `data.request_id` is a fresh hex UUID, `data.raw_input` is the verbatim original user message.
4. Polling `GET /v1/extraction/{request_id}` eventually returns the real envelope with `status in {"completed", "failed"}`.

### Internal behavior (CHANGED)

Before M1: `ExtractionService.run()` internally called `asyncio.create_task(self._run_background(...))` and returned an already-pending envelope. The service owned the background task.

After M1: `ExtractionService.run()` awaits the full pipeline **inline** and returns a terminal envelope (`completed` or `failed`). The background-task scheduling moves UP to `ChatService._dispatch_extraction`:

```python
async def _dispatch_extraction(self, request: ChatRequest) -> ChatResponse:
    request_id = uuid4().hex
    asyncio.create_task(
        self._extract_and_persist(
            raw_input=request.message,
            user_id=request.user_id,
            request_id=request_id,
        )
    )
    pending = ExtractPlaceResponse(
        status="pending",
        results=[],
        raw_input=request.message,
        request_id=request_id,
    )
    return ChatResponse(
        type="extract-place",
        message="On it — extracting the place in the background. Check back in a moment.",
        data=pending.model_dump(mode="json"),
    )

async def _extract_and_persist(
    self, raw_input: str, user_id: str, request_id: str
) -> None:
    try:
        response = await self._extraction.run(raw_input, user_id)
        # ExtractionService.run() has already written to Redis under
        # extraction:v2:{request_id_from_run}. Our outer request_id is what
        # was returned to the user; it may differ from whatever run()
        # generates internally. Contract: write under OUR request_id so the
        # polling route can find it.
        await self._status_repo.write(
            request_id,
            response.model_dump(mode="json"),
        )
    except Exception:
        logger.exception("Background extraction failed for request %s", request_id)
```

**Note on request_id ownership**: Two competing options exist:

- **Option A**: `ExtractionService.run()` accepts an injected `request_id` and uses it for its internal Redis write.
- **Option B**: `ExtractionService.run()` generates its own `request_id`, returns it on the envelope; the route layer writes the real envelope under the route-owned `request_id` too.

Option A is cleaner and matches the plan's example. During implementation, pass the route-generated `request_id` into `ExtractionService.run(...)` (widen the signature by one keyword arg) so both the Redis write and the envelope carry the same id. This avoids the dual-write in `_extract_and_persist` above.

## Contract points

| Point | Pre-M1 | Post-M1 |
|---|---|---|
| Who schedules the background task | `ExtractionService.run()` | `ChatService._dispatch_extraction` |
| Who writes the terminal envelope to Redis | `_run_background` (deleted) | `ExtractionService.run()` inline (FR-010) |
| Who returns the `pending` envelope to the caller | `ExtractionService.run()` (eagerly) | `ChatService._dispatch_extraction` (via create_task) |
| `ExtractionService.run()` return `status` values | `pending` / `completed` / `failed` | `completed` / `failed` only |
| How `ChatService` detects terminal vs pending | `any(r.status == "pending" for r in results)` | `extract_result.status == "pending"` at the envelope (FR-012) |
| `_outcome_to_dict` → `_outcome_to_item_dict` rename | N/A | Only maps real outcomes; `_is_real` filters below-threshold (FR-013) |

## Behavior under failure

- `_extract_and_persist` wraps `await self._extraction.run(...)` in try/except. On exception, logs via `logger.exception` and does NOT write anything to Redis — the polling route returns 404 until the TTL-less "missing key" path. This is already the current behavior (see `_run_background` in the pre-M1 service).
- `ExtractionService.run()`'s inline await inherits the pipeline's own failure semantics (Whisper timeout, vision timeout, Google Places failure) — it should NOT propagate those up. It catches and collapses to `status="failed", results=[]`.

## Tests — acceptance

- `tests/core/chat/test_service.py::test_dispatch_extraction_returns_pending_synchronously` — mock ExtractionService to `await asyncio.sleep(10)`, assert `_dispatch_extraction` returns within the test's default 2s window with `data.status="pending"`.
- `tests/core/chat/test_service.py::test_dispatch_extraction_background_writes_real_envelope` — mock ExtractionService to return `ExtractPlaceResponse(status="completed", results=[...])`, assert the status repo has a write with the same `request_id` and payload.
- `tests/core/chat/test_service.py::test_dispatch_extraction_raw_input_is_verbatim` — submit a message with leading whitespace and a tracking URL, assert `data.raw_input` is byte-identical to the input.
