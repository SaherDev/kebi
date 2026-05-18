# API Contract — product repo ↔ kebi

Source of truth: the product repo's `docs/api-contract.md`. Copy to `kebi/docs/` after any changes.

This document defines the HTTP contract between the product repo (services/api) and the AI service (kebi). The product repo is the client. The AI repo is the server.

All requests come from NestJS after auth verification. kebi never receives requests directly from the frontend.

## Connection

- Base URL loaded from YAML config: `ai_service.base_url`
- All endpoints are prefixed with `/v1/`
- Most requests are JSON over HTTP (`Content-Type: application/json`)
- `POST /v1/chat/stream` uses Server-Sent Events (`Content-Type: text/event-stream`) — NestJS must forward the stream to the frontend without buffering
- Auth between services is TBD (likely a shared secret header in later phases)

---

## Shared Types

### `PlaceObject`

Unified place shape returned by every read and write path (ADR-054, feat 019). Tier 1 fields come from PostgreSQL and are always present; Tier 2 (Redis geo) and Tier 3 (Redis enrichment) populate only when `enrich_batch` ran.

```json
{
  "place_id": "pl_01HZ...",
  "place_name": "Nara Eatery",
  "place_type": "food_and_drink",
  "subcategory": "restaurant",
  "tags": ["ramen", "late_night"],
  "attributes": {
    "cuisine": "japanese",
    "price_hint": "$$",
    "ambiance": "casual",
    "dietary": ["vegetarian"],
    "good_for": ["date_night"],
    "location_context": {
      "neighborhood": "Ari",
      "city": "Bangkok",
      "country": "TH"
    }
  },
  "source_url": "https://tiktok.com/@user/video/123",
  "source": "tiktok",
  "provider_id": "google:ChIJN1t_tDeuEmsRUsoyG83frY4",
  "created_at": "2026-04-12T10:15:00Z",

  "lat": 13.778,
  "lng": 100.541,
  "address": "123 Ari Soi 4, Bangkok 10400",
  "geo_fresh": true,

  "hours": { "monday": "11:00-22:00", "timezone": "Asia/Bangkok" },
  "rating": 4.6,
  "phone": "+66 2 123 4567",
  "photo_url": "https://places.googleapis.com/...",
  "popularity": 0.82,
  "enriched": true
}
```

| Tier | Field         | Type                                                                                | Notes                                                                                 |
| ---- | ------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| 1    | `place_id`    | `string`                                                                            | Internal UUID/ULID; always present                                                    |
| 1    | `place_name`  | `string`                                                                            | Always present                                                                        |
| 1    | `place_type`  | `"food_and_drink" \| "things_to_do" \| "shopping" \| "services" \| "accommodation"` | Enum                                                                                  |
| 1    | `subcategory` | `string \| null`                                                                    | Validated against the per-type vocabulary (e.g. `"restaurant"`, `"cafe"`, `"museum"`) |
| 1    | `tags`        | `string[]`                                                                          | Free-form labels                                                                      |
| 1    | `attributes`  | `PlaceAttributes`                                                                   | Structured JSONB; shape below                                                         |
| 1    | `source_url`  | `string \| null`                                                                    | Original URL the place was extracted from                                             |
| 1    | `source`      | `"tiktok" \| "instagram" \| "youtube" \| "manual" \| "link" \| "consult" \| null`   | Acquisition channel                                                                   |
| 1    | `provider_id` | `string \| null`                                                                    | Namespaced external ID (e.g. `"google:ChIJ…"`); constructed only by the repository    |
| 1    | `created_at`  | `ISO-8601 string \| null`                                                           | From ORM row; `null` for freshly-built, unsaved objects                               |
| 2    | `lat` / `lng` | `float \| null`                                                                     | Populated when geo cache is hydrated                                                  |
| 2    | `address`     | `string \| null`                                                                    | Formatted address                                                                     |
| 2    | `geo_fresh`   | `bool`                                                                              | `true` when Tier 2 fields come from a live cache hit                                  |
| 3    | `hours`       | `object \| null`                                                                    | Keys: `sunday`–`saturday` (string or null) + `timezone` (IANA)                        |
| 3    | `rating`      | `float \| null`                                                                     | Provider rating                                                                       |
| 3    | `phone`       | `string \| null`                                                                    | E.164 preferred                                                                       |
| 3    | `photo_url`   | `string \| null`                                                                    | Hotlinkable image URL                                                                 |
| 3    | `popularity`  | `float \| null`                                                                     | Normalized 0–1                                                                        |
| 3    | `enriched`    | `bool`                                                                              | `true` when Tier 3 fields are populated. Recall-mode responses never set this `true`  |

`PlaceAttributes` shape:

| Field              | Type                                      | Notes                                |
| ------------------ | ----------------------------------------- | ------------------------------------ |
| `cuisine`          | `string \| null`                          | e.g. `"japanese"`                    |
| `price_hint`       | `string \| null`                          | e.g. `"$"`, `"$$"`, `"$$$"`          |
| `ambiance`         | `string \| null`                          | e.g. `"casual"`, `"upscale"`         |
| `dietary`          | `string[]`                                | e.g. `["vegetarian", "gluten_free"]` |
| `good_for`         | `string[]`                                | e.g. `["date_night", "groups"]`      |
| `location_context` | `{ neighborhood, city, country } \| null` | All string or null                   |

---

## POST /v1/chat

Unified conversational entry point (ADR-052). Replaces all four former individual endpoints.
The system classifies intent, dispatches to the correct pipeline, and returns a structured response.

**Request:**

```json
{
  "user_id": "<user_id>",
  "message": "cheap dinner nearby",
  "location": { "lat": 13.7563, "lng": 100.5018 }
}
```

| Field         | Type                                                          | Required | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ------------- | ------------------------------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `user_id`     | `string`                                                      | Yes      | Clerk-issued user ID; trusted, not validated here                                                                                                                                                                                                                                                                                                                                                                                                |
| `message`     | `string`                                                      | Yes      | Natural language message from the user                                                                                                                                                                                                                                                                                                                                                                                                           |
| `location`    | `{ lat: float, lng: float }`                                  | No       | Passed to consult pipeline only; ignored for all other intents                                                                                                                                                                                                                                                                                                                                                                                   |

**Response:**

```json
{
  "type": "agent",
  "message": "Based on what I know about you, try Nara Eatery…",
  "data": {},
  "tool_calls_used": 2
}
```

| Field             | Type             | Notes                                                                                                          |
| ----------------- | ---------------- | -------------------------------------------------------------------------------------------------------------- |
| `type`            | `string`         | One of: `agent`, `consult`, `recall`, `clarification`, `error` (ADR-073 removed `extract-place` — saves no longer flow through chat) |
| `message`         | `string`         | Human-readable response text                                                                                   |
| `data`            | `object \| null` | Structured payload from downstream service; shape varies by `type` — see sections below                        |
| `tool_calls_used` | `integer`        | Count of tool invocations (recall, consult) during this turn. Always present; `0` when no tools ran. Read by NestJS to increment the daily tool-call counter. |

**Response Types by Intent:**

### `consult`

```json
{
  "type": "consult",
  "message": "Here's my top pick for dinner nearby",
  "data": {
    "recommendation_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "results": [
      {
        "place": {
          /* PlaceObject — fully enriched (enriched=true) */
        },
        "source": "saved"
      }
    ],
    "reasoning_steps": [
      { "step": "parse_intent", "summary": "Detected: dinner, nearby, cheap" },
      {
        "step": "retrieve_candidates",
        "summary": "12 saved + 8 discovered within 2km"
      }
    ]
  }
}
```

`data` (`ConsultResponse`):

| Field               | Type              | Notes                                                                                             |
| ------------------- | ----------------- | ------------------------------------------------------------------------------------------------- |
| `recommendation_id` | `string \| null`  | UUID of the persisted row in `recommendations`. `null` if the background persist failed (ADR-060) |
| `results`           | `ConsultResult[]` | Up to 3 items. Agent-driven ranking; order is not score-derived (ADR-058)                         |
| `reasoning_steps`   | `ReasoningStep[]` | Flat trace for eval/debug; UI does not need to render                                             |

`ConsultResult` shape:

| Field    | Type                      | Notes                                                                                 |
| -------- | ------------------------- | ------------------------------------------------------------------------------------- |
| `place`  | `PlaceObject`             | Always fully enriched (`enriched=true`, Tier 2 + Tier 3 populated)                    |
| `source` | `"saved" \| "discovered" \| "suggested"` | `"saved"` = from user's recall set; `"discovered"` = from keyword search; `"suggested"` = agent-supplied name validated via places provider |

`ReasoningStep` shape:

| Field        | Type                                              | Notes                                                                                                                                                   |
| ------------ | ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `step`       | `string`                                          | Identifier: `agent.tool_decision`, `tool.summary`, `fallback`, `max_steps_detail`, etc.                                                                 |
| `summary`    | `string`                                          | Human-readable description. For `agent.tool_decision` steps this is the LLM's reasoning text **truncated to 200 chars** — do not use as the full response |
| `source`     | `"tool" \| "agent" \| "fallback"`                 | Which part of the graph produced this step                                                                                                              |
| `tool_name`  | `"recall" \| "consult" \| null`         | Set iff `source == "tool"`, `null` otherwise (ADR-073 removed `"save"`)                                                                                                            |
| `visibility` | `"user" \| "debug"`                               | Only `"user"` steps appear in API responses; `"debug"` steps go to Langfuse/SSE only                                                                   |
| `duration_ms`| `float`                                           | Latency in ms. `0` for `agent.tool_decision` steps (agent node doesn't measure LLM latency); populated for tool steps                                   |
| `timestamp`  | `ISO-8601 string`                                 | UTC timestamp when the step was recorded                                                                                                                |

### `recall`

```json
{
  "type": "recall",
  "message": "Found 3 places matching your search",
  "data": {
    "results": [
      {
        "place": {
          /* PlaceObject — Tier 2 may be present; enriched=false */
        },
        "match_reason": "semantic + keyword",
        "relevance_score": 0.0187,
        "score_type": "rrf"
      }
    ],
    "total_count": 3,
    "empty_state": false
  }
}
```

`data` (`RecallResponse`):

| Field         | Type             | Notes                                                                        |
| ------------- | ---------------- | ---------------------------------------------------------------------------- |
| `results`     | `RecallResult[]` | Ordered by relevance when a query is present; by recency in filter-only mode |
| `total_count` | `integer`        | Pre-`LIMIT` match count. Post-distance-filter this is best-effort            |
| `empty_state` | `bool`           | `true` only when the user has zero saved places                              |

`RecallResult` shape:

| Field             | Type                                                          | Notes                                                                             |
| ----------------- | ------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| `place`           | `PlaceObject`                                                 | Recall never enriches Tier 3 — `enriched` is always `false`                       |
| `match_reason`    | `"filter" \| "semantic" \| "keyword" \| "semantic + keyword"` | Which retrieval path surfaced the row                                             |
| `relevance_score` | `float \| null`                                               | Populated only in hybrid mode. Scale depends on `score_type`                      |
| `score_type`      | `"rrf" \| "ts_rank" \| null`                                  | `rrf` scores are ~0.01–0.03; `ts_rank` scores are 0–1. Never compare across types |

### `agent`

Primary response type since M11 (ADR-065). Returned for all conversational turns that go through the LangGraph agent.

```json
{
  "type": "agent",
  "message": "Here are some ramen spots near you.",
  "data": {
    "reasoning_steps": [
      {
        "step": "agent.tool_decision",
        "summary": "recall — user referenced saved places",
        "source": "agent",
        "tool_name": null,
        "visibility": "user",
        "duration_ms": 0.0,
        "timestamp": "2026-04-23T10:00:00Z"
      }
    ],
    "tool_results": [
      {
        "tool": "recall",
        "tool_call_id": "toolu_01abc...",
        "payload": { /* RecallResponse shape */ }
      }
    ]
  },
  "tool_calls_used": 2
}
```

`data` fields:

| Field             | Type               | Notes                                                                                     |
| ----------------- | ------------------ | ----------------------------------------------------------------------------------------- |
| `reasoning_steps` | `ReasoningStep[]`  | Only `visibility="user"` steps — `agent.tool_decision`, `tool.summary`, `fallback`        |
| `tool_results`    | `ToolResult[]`     | Structured payloads from relevant tool calls this turn. When recall + consult both ran, only consult is included — consult already merges saved + discovered results so the recall payload is redundant. Empty array when no tools ran. |

`ToolResult` shape:

| Field          | Type             | Notes                                                                  |
| -------------- | ---------------- | ---------------------------------------------------------------------- |
| `tool`         | `string \| null` | Tool name: `"recall"` or `"consult"` (ADR-073 removed `"save"`)                        |
| `tool_call_id` | `string \| null` | LangGraph tool call identifier                                         |
| `payload`      | `object \| null` | Parsed JSON from the tool's service response; `null` if unparseable    |

### `clarification`

Reserved for future agent-side clarification flows. Not currently emitted — the save-time `needs_review` NodeInterrupt that previously triggered this was removed by ADR-071, and the save tool itself was removed by ADR-073.

### `error`

```json
{
  "type": "error",
  "message": "Something went wrong, try again",
  "data": { "detail": "..." }
}
```

| Field         | Type     | Notes                                                     |
| ------------- | -------- | --------------------------------------------------------- |
| `data.detail` | `string` | Internal detail string for logs; safe to ignore in the UI |

All downstream exceptions are caught and surfaced as `type="error"` with HTTP 200 (not 5xx).

**HTTP Status Codes:**

| Code  | When                                             |
| ----- | ------------------------------------------------ |
| `200` | All successful responses including clarification |
| `400` | Malformed request body                           |
| `422` | Validation error (FastAPI auto, per ADR-023)     |
| `500` | Unhandled internal error                         |

**Notes:**

- `location` is forwarded to the agent and available to the consult tool; ignored for direct-response turns.
- All downstream exceptions are caught and returned as `type="error"` with HTTP 200 (not 5xx).
- Consult results are persisted to the `recommendations` table after a successful response (ADR-060). The response includes a `recommendation_id` (UUID) referencing the persisted row. Write failures are logged but do not fail the caller response — `recommendation_id` will be `null` in that case.

---

## POST /v1/chat/stream

SSE streaming variant of the chat endpoint. Emits reasoning steps as they happen, then a final message frame and a done frame. Requires the agent to be enabled.

**Request:** Identical body to `POST /v1/chat`.

**Response:** `Content-Type: text/event-stream`. Four frame types, emitted in this order:

```
event: reasoning_step
data: <ReasoningStep JSON>

event: tool_result
data: {"tool": "consult", "tool_call_id": "toolu_01...", "payload": { /* ConsultResponse */ }}

event: message
data: {"content": "<final assistant text>"}

event: done
data: {"tool_calls_used": 2}
```

| Frame            | When emitted                                                           | Data shape                                                        |
| ---------------- | ---------------------------------------------------------------------- | ----------------------------------------------------------------- |
| `reasoning_step` | Each time a tool or agent node emits a custom event (real-time)        | `ReasoningStep` (all fields)                                      |
| `tool_result`    | After graph completes — one frame per relevant tool result (see below) | `{"tool": string, "tool_call_id": string, "payload": object}`     |
| `message`        | Once, after the graph completes, if there is text                      | `{"content": string}`                                             |
| `done`           | Always last — even if no message was produced                          | `{"tool_calls_used": integer}`                                    |

**Tool result suppression rule** (applies to both SSE and non-SSE):

- **Recall + consult turn** — only the `consult` tool_result is emitted. Recall is an internal step; consult already merges saved + discovered results.
- **Recall-only turn** — the `recall` tool_result is emitted.

**Expected client behaviour per frame type:**

| Frame            | Client action                                                                 |
| ---------------- | ----------------------------------------------------------------------------- |
| `reasoning_step` | Show progress indicator (e.g. "Checking your saves…", "Ranking options…")    |
| `tool_result`    | Render place cards as soon as they arrive — don't wait for `message`          |
| `message`        | Render the final text bubble                                                  |
| `done`           | Capture `tool_calls_used` for rate-limit tracking, then close the stream      |

On error mid-stream:

```
event: error
data: {"detail": "<error string>"}
```

**HTTP Status Codes:**

| Code | When                                          |
| ---- | --------------------------------------------- |
| `200`| Streaming started successfully                |
| `400`| Agent disabled or graph unavailable           |

---

## POST /v1/extract

Canonical product-facing extraction endpoint (ADR-073). The product repo calls this directly whenever a user submits a URL or place name to save. `/v1/chat` is conversation-only and does not write to `user_places`.

**Request:**

```json
{
  "raw_input": "https://www.tiktok.com/@user/video/123",
  "user_id": "user-uuid"
}
```

| Field       | Type     | Required | Description                                                          |
| ----------- | -------- | -------- | -------------------------------------------------------------------- |
| `raw_input` | `string` | yes      | URL (TikTok / Instagram / YouTube / Google Maps list) or place name. |
| `user_id`   | `string` | yes      | The submitting user's ID. Used as the `user_places` owner.           |

**Response (200):** `ExtractPlaceResponse` — `places: list[ExtractPlaceItem]` with full evidence trail per item. Per ADR-071 every picker candidate is persisted with `approved=False`; the user curates after the fact via the product UI.

**Latency profile:**

- Text input (a single place name) → typically <1 s.
- URL input (TikTok/Instagram caption only) → 2–5 s.
- URL input (video that needs yt-dlp + Whisper + vision frames) → 30–60 s. The client should show a progress indicator and tolerate a long synchronous response.

**HTTP Status Codes:**

| Code  | When                                                                  |
| ----- | --------------------------------------------------------------------- |
| `200` | Extraction completed — response includes any saved places + duplicates |
| `400` | Malformed request (missing `raw_input` / `user_id`, or `raw_input` exceeds the size cap) |
| `500` | Pipeline failure — see `error` field in response                       |

---

## GET /v1/extraction/{request_id}

**Reserved for future async use.** No product flow writes status keys today — the canonical extraction path is synchronous `POST /v1/extract`. This route remains so a future async/background variant can plug in without API churn.

**Request:** No body. `request_id` is a path parameter.

**Response (200):** `ExtractPlaceResponse` — same shape as `POST /v1/extract`.

**HTTP Status Codes:**

| Code  | When                                                       |
| ----- | ---------------------------------------------------------- |
| `200` | Result found in Redis                                      |
| `404` | Key not found — still running, or TTL expired              |

Extraction results would be cached under the `extraction:v2:{request_id}` key prefix (ADR-063).

---

## DELETE /v1/user/{user_id}/data

Hard-deletes every trace of a user's **AI-owned data**. Does NOT delete the user account — that lives in NestJS/Clerk. Called by the product repo as part of its account-deletion flow: NestJS deletes its own `users` / `user_settings` rows and calls this endpoint to wipe the AI side.

**Request:**

```
DELETE /v1/user/user_abc/data
DELETE /v1/user/user_abc/data?scope=chat_history
DELETE /v1/user/user_abc/data?scope=chat_history&scope=all
```

**Response (204):** Empty body.

**Optional query params:**

| Param | Type | Description |
| ----- | ---- | ----------- |
| `scope` | repeated `DataScope` enum (`all` \| `chat_history`) | Selects what to delete. Repeat the param for multi-value. Omit to wipe everything (default; preserves the NestJS account-delete contract). Unknown values → 422 (FastAPI native enum validation). |

`DataScope` values:

- **`all`** (default) — full wipe across all 7 deletion targets below. Same as omitting the query param.
- **`chat_history`** — clears only target #6 (LangGraph checkpoint thread) and target #7 (pending taste-regen task). Leaves all SQL data (places, taste_model, memories, interactions, recommendations) intact. Useful for resetting an agent that learned a stale pattern (e.g. "this URL always times out") without losing the user's saves.

A scope set containing `all` collapses to "wipe everything" regardless of any other scopes mixed in.

**What gets deleted (default / `scope=all`, in one DB transaction, then the checkpointer, then the in-memory debouncer):**

1. `interactions` rows where `user_id = ?`
2. `recommendations` rows where `user_id = ?`
3. `user_memories` rows where `user_id = ?`
4. `taste_model` row for `user_id = ?` (one per user)
5. `places` rows where `user_id = ?` — `embeddings` cascade automatically via FK `ON DELETE CASCADE`
6. LangGraph checkpoint thread for `thread_id = user_id` (= the agent conversation history across all turns)
7. Any pending taste-regen task for the user in the in-memory debouncer

**Notes:**

- **Idempotent.** Calling on an absent user is still 204. The SQL deletes return 0 rows; `adelete_thread` is a no-op when the thread isn't there.
- **Synchronous.** The endpoint blocks until the sweep completes. At portfolio volume the sweep is well under 1s. If a single user ever accumulates thousands of places this can move to a background task with a 202 response — see plan.
- **Hard-delete only.** No soft-delete column, no grace window, no purge cron. This is a deliberate v1 simplification — soft-delete on `places` can be added later if a real GDPR / "oops recovery" requirement appears.
- **No Redis cleanup.** All Redis keys in this repo are `place_id` or `request_id` scoped (see ADR-054); none are per-user.
- **Caller trust.** No new auth — this matches every other route's "NestJS verified upstream" model.
- **NestJS account-delete still uses no `scope`.** The product-side contract documents only the default full-wipe; `scope=chat_history` is reserved for direct frontend "Reset chat history" flows or future product features that NestJS proxies.

---

## POST /v1/signal

Behavioral signal endpoint (ADR-060). Replaces `POST /v1/feedback`. Carries recommendation accept/reject feedback.

**Request:**

```json
{
  "signal_type": "recommendation_accepted",
  "user_id": "<user_id>",
  "recommendation_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "place_core_id": "c0ffee00-1111-2222-3333-444455556666"
}
```

| Field               | Type     | Required | Notes                                                      |
| ------------------- | -------- | -------- | ---------------------------------------------------------- |
| `signal_type`       | `string` | Yes      | `"recommendation_accepted"` or `"recommendation_rejected"` |
| `user_id`           | `string` | Yes      | Clerk-issued user ID                                       |
| `recommendation_id` | `string` | Yes      | Must exist in recommendations table                        |
| `place_core_id`     | `string` | Yes      | `places_v2.id` of the place (ADR-077; renamed from `place_id` to disambiguate from `user_place_id` / `provider_id`). Trusted, not validated. |

**Responses:** `202 { "status": "accepted" }`; `404` if recommendation_id unknown; `422` on schema errors (including unknown `signal_type`).

---

## GET /v1/health

Health check endpoint. Returns service status and database connectivity.

**Request:** No parameters, no body.

**Response (200):**

```json
{
  "status": "ok",
  "name": "kebi",
  "version": "0.1.0",
  "db": "connected"
}
```

| Field     | Type                            | Notes                                                                          |
| --------- | ------------------------------- | ------------------------------------------------------------------------------ |
| `status`  | `string`                        | Always `"ok"` when the service is up and this handler is reachable             |
| `name`    | `string`                        | App name from `config/app.yaml` (`app.name`)                                   |
| `version` | `string`                        | Package version from installed metadata; falls back to `"0.1.0"` if unresolved |
| `db`      | `"connected" \| "disconnected"` | Result of a `SELECT 1` probe against the primary PostgreSQL connection         |

Always HTTP 200 — DB outages surface via `db: "disconnected"`, not a non-2xx status.

---

## API Contract Summary

| Endpoint                          | Purpose                                 | NestJS Sends                                                                                                                                                    | kebi Returns                                                                        |
| --------------------------------- | --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| POST /v1/chat                     | Unified conversational entry point      | user_id, message, optional location                                                                                                                             | type, message, data, tool_calls_used                                                     |
| POST /v1/chat/stream              | SSE streaming chat                      | Same as POST /v1/chat                                                                                                                                           | reasoning_step frames, message frame, done frame (tool_calls_used)                       |
| POST /v1/extract                  | Canonical extraction (save a place)     | raw_input, user_id                                                                                                                                              | ExtractPlaceResponse                                                                     |
| GET /v1/extraction/{request_id}   | Poll background extraction (reserved)   | request_id (path param)                                                                                                                                         | ExtractPlaceResponse (200) or 404                                                        |
| POST /v1/signal                   | Recommendation accept/reject feedback   | `signal_type` (recommendation_accepted/rejected), recommendation_id, place_core_id                                                                               | status (202)                                                                             |
| GET /v1/health                    | Service health check                    | —                                                                                                                                                               | status, db connectivity                                                                  |

---

## Error Handling

The AI service returns standard HTTP status codes:

| Status  | Meaning                                                    | Product repo action                                     |
| ------- | ---------------------------------------------------------- | ------------------------------------------------------- |
| 200     | Success (including clarification and error type responses) | Process response                                        |
| 400     | Bad request (malformed input)                              | Log error, return 400 to frontend                       |
| 422     | Validation error                                           | Return friendly message to frontend                     |
| 500     | AI service internal error                                  | Log error, return 503 to frontend with retry suggestion |
| Timeout | Service unreachable                                        | Return 503 with "service temporarily unavailable"       |

**Timeout policy:** Set HTTP client timeout to 30 seconds for all AI service calls. /v1/chat responses targeting consult intent may take up to 20s for complex queries.

---

## Shared Configuration

These values must stay in sync between both repos. A mismatch breaks the system.

**Embedding dimensions:**

- Current: 1024 (Voyage 4-lite)
- pgvector columns are fully owned by this repo's Alembic migrations — NestJS never defines vector columns
- If the embedding model changes, only this repo's Alembic migration and config need updating

**Database tables FastAPI writes to:**

- places
- embeddings
- taste_model
- recommendations (ADR-060 — AI recommendation history, renamed from consult_logs)
- user_memories (personal facts extracted from chat messages)
- interaction_log (append-only behavioral signal log)

Alembic in kebi owns migrations for these tables. NestJS never touches them. If the schema changes, run the migration from kebi only.

---

## General Notes

- All requests include `user_id` so FastAPI can load user-specific taste models and saved places.
- FastAPI writes AI-generated data (places, embeddings, taste model, recommendations) directly to PostgreSQL.
- NestJS writes product data (users, settings) to PostgreSQL.
- Neither service writes to the other's tables.
- The product repo is responsible for auth and validating `user_id` before calling these endpoints.
