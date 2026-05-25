# API Contract — product repo ↔ kebi

Source of truth: the product repo's `docs/api-contract.md`. Copy to `kebi/docs/` after any changes.

This document defines the HTTP contract between the product repo (services/api) and the AI service (kebi). The product repo is the client. The AI repo is the server.

All requests come from NestJS after auth verification. kebi never receives requests directly from the frontend.

> **ADR-079 coordination note.** The place catalog tables were renamed
> `places_v2 → places` and `place_embeddings_v2 → place_embeddings`
> (ADR-079, from `places_v2` introduced in ADR-070). The kebi schema +
> code and any product-repo reference to these table names must ship in
> **one coordinated deploy**. `place_core_id` on `POST /v1/signal` is
> now documented as `places.id`.

## Connection

- Base URL loaded from YAML config: `ai_service.base_url`
- All endpoints are prefixed with `/v1/`
- Most requests are JSON over HTTP (`Content-Type: application/json`)
- `POST /v1/chat/stream` uses Server-Sent Events (`Content-Type: text/event-stream`) — NestJS must forward the stream to the frontend without buffering
- Auth between services is a trusted-upstream model: NestJS verifies the user and forwards `user_id`; kebi does not re-authenticate

---

## Shared Types

### `PlaceCore`

The canonical place shape (ADR-070, ADR-077; the catalog table is
`places` since ADR-079). Identity + static catalog fields only.
Extraction returns `PlaceCore` — it does **not** populate live fields
(rating, hours, popularity, business_status); those are filled in later
by the catalog read/enrichment path, which has no product-facing
endpoint today (ADR-075 removed recall/consult).

```json
{
  "id": "c0ffee00-1111-2222-3333-444455556666",
  "provider_id": "google:ChIJN1t_tDeuEmsRUsoyG83frY4",
  "place_name": "Nara Eatery",
  "place_name_aliases": [
    { "value": "Nara", "source": "tiktok" }
  ],
  "categories": ["restaurant"],
  "tags": [
    { "type": "cuisine", "value": "Japanese", "source": "google" },
    { "type": "atmosphere", "value": "casual", "source": "llm" }
  ],
  "location": {
    "lat": 13.778, "lng": 100.541,
    "address": "123 Ari Soi 4, Bangkok 10400",
    "neighborhood": "Ari", "city": "Bangkok", "country": "TH"
  },
  "created_at": "2026-04-12T10:15:00Z",
  "refreshed_at": "2026-05-01T08:00:00Z"
}
```

| Field                | Type                                  | Notes                                                                                       |
| -------------------- | ------------------------------------- | ------------------------------------------------------------------------------------------- |
| `id`                 | `string \| null`                      | Catalog primary key (`places.id`). `null` only for freshly-built, unsaved objects           |
| `provider_id`        | `string \| null`                      | Namespaced external ID (e.g. `"google:ChIJ…"`)                                               |
| `place_name`         | `string`                              | Canonical name (provider-sourced)                                                           |
| `place_name_aliases` | `{ value, source }[]`                 | Alternative names from non-canonical writers (TikTok caption, user note, LLM)                |
| `categories`         | `string[]`                            | `PlaceCategory` enum values, e.g. `"restaurant"`, `"cafe"`, `"bar"`                          |
| `tags`               | `{ type, value, source }[]`           | `type` ∈ `cuisine \| dietary \| feature \| atmosphere \| service \| price \| accessibility \| time \| season` (or an LLM free-text type); `value` is an enum or free-text; `source` e.g. `"google" \| "llm" \| "tiktok"` |
| `location`           | `LocationContext \| null`             | `{ lat, lng, address, neighborhood, city, country }` — any field may be `null`              |
| `created_at`         | `ISO-8601 string \| null`             | Catalog row creation                                                                        |
| `refreshed_at`       | `ISO-8601 string \| null`             | Last provider refresh                                                                       |

> **Migration note (ADR-070/071):** the legacy v1 `PlaceObject` shape
> (`place_type`, `subcategory`, `attributes{}`, Tier 2/3 enrichment
> fields) is gone. `place_type` → `categories: string[]`; `attributes`
> → `tags: [{type,value,source}]`. The v1 `places`/`embeddings` tables
> were dropped in ADR-078.

---

## POST /v1/chat

Unified conversational entry point (ADR-052, ADR-065). The agent is a
**zero-tool conversational Q&A surface** (ADR-075): it answers from
general knowledge plus the user's taste/memory context, and redirects
place save / recall / recommendation requests to the product's own
surfaces (e.g. the share/submit input → `POST /v1/extract`). It performs
no retrieval and writes no data.

**Request:**

```json
{
  "user_id": "<user_id>",
  "message": "somewhere for cheap dinner near me",
  "location": { "lat": 13.7563, "lng": 100.5018 },
  "movement_profile": {
    "available_modes": ["walking", "transit", "motorbike"],
    "reach": "normal"
  }
}
```

| Field              | Type                         | Required | Notes                                                                                 |
| ------------------ | ---------------------------- | -------- | ------------------------------------------------------------------------------------- |
| `user_id`          | `string`                     | Yes      | Clerk-issued user ID; trusted, not validated here                                     |
| `message`          | `string`                     | Yes      | Natural-language message from the user                                                |
| `location`         | `{ lat: float, lng: float }` | No       | The user's **actual** location — where they physically are. ADR-083 makes this the anchor for per-turn working-location resolution: the agent resolves the location a turn operates against (a place named in the message, one carried from the conversation, or this actual location as fallback) and reverse-geocodes these coords when they are used. Shape unchanged |
| `movement_profile` | `MovementProfile \| null`    | No       | The user's mobility capability (ADR-084 + ADR-085) — owned by the product repo's `user_settings`, sent each turn like `location`. `{ available_modes, reach }`. `available_modes` items ∈ `walking \| cycling \| motorbike \| driving \| transit \| rideshare`; list is non-empty and represents modes the user *can* use (licence, owned vehicles, comfort) — NOT per-city availability. `reach` ∈ `compact \| normal \| far`, default `normal`. Omitted → kebi applies a neutral fallback. The agent resolves an effective mode per turn by pairing this capability with the working location's city + density; an explicit mode word in the message still overrides. It never mutates the profile. A stray `default_mode` key (from a pre-ADR-085 client) is silently ignored |

**Response:**

```json
{
  "type": "agent",
  "message": "For ramen, look for a shop that…",
  "data": { "reasoning_steps": [] },
  "tool_calls_used": 0
}
```

| Field             | Type             | Notes                                                                                          |
| ----------------- | ---------------- | ---------------------------------------------------------------------------------------------- |
| `type`            | `string`         | One of `agent`, `error`. ADR-075 removed `consult`/`recall`; ADR-073 had removed `extract-place`/`clarification`. No other values are emitted. |
| `message`         | `string`         | Human-readable response text                                                                   |
| `data`            | `object \| null` | `agent`: `{ "reasoning_steps": ReasoningStep[] }` (user-visible steps only). `error`: `{ "detail": string }` |
| `tool_calls_used` | `integer`        | **Always `0`** — the agent has no tools (ADR-075). Retained for response-shape stability        |

`ReasoningStep` shape:

| Field         | Type                          | Notes                                                                       |
| ------------- | ----------------------------- | --------------------------------------------------------------------------- |
| `step`        | `string`                      | Identifier, e.g. `agent.response`, `fallback`                               |
| `summary`     | `string`                      | Human-readable description (LLM reasoning text may be truncated)            |
| `source`      | `"agent" \| "fallback"`       | Which node produced it. No `"tool"` source since ADR-075; `tool_name` is gone |
| `visibility`  | `"user" \| "debug"`           | Only `"user"` steps appear in the JSON response; `"debug"` → Langfuse/SSE   |
| `timestamp`   | `ISO-8601 string`             | UTC; when the step was recorded                                            |
| `duration_ms` | `float \| null`               | Node latency; non-null in persisted steps                                  |

### `error`

```json
{ "type": "error", "message": "Something went wrong, try again", "data": { "detail": "..." } }
```

`data.detail` is an internal string for logs — safe to ignore in the UI. All downstream exceptions are caught and surfaced as `type="error"` with **HTTP 200** (not 5xx).

**HTTP Status Codes:**

| Code  | When                                         |
| ----- | -------------------------------------------- |
| `200` | All successful responses, including `error`  |
| `400` | Malformed request body                       |
| `422` | Validation error (FastAPI auto, per ADR-023) |
| `500` | Unhandled internal error                     |

---

## POST /v1/chat/stream

SSE streaming variant. Emits reasoning steps as they happen, then a final message frame and a done frame. Requires the agent to be enabled.

**Request:** Identical body to `POST /v1/chat`.

**Response:** `Content-Type: text/event-stream`. Frame types, in this order:

```
event: reasoning_step
data: <ReasoningStep JSON>

event: message
data: {"content": "<final assistant text>"}

event: done
data: {"tool_calls_used": 0}
```

| Frame            | When emitted                                            | Data shape                       |
| ---------------- | ------------------------------------------------------- | -------------------------------- |
| `reasoning_step` | Each time the agent/fallback node emits a custom event  | `ReasoningStep` (all fields)     |
| `message`        | Once, after the graph completes, if there is text       | `{"content": string}`            |
| `done`           | Always last — even if no message was produced           | `{"tool_calls_used": 0}`         |

> ADR-075 removed all tools, so the stream emits **no `tool_result`
> frames**. Any prior recall/consult tool-result handling in the client
> is dead and should be removed.

On error mid-stream:

```
event: error
data: {"detail": "<error string>"}
```

| Code  | When                                |
| ----- | ----------------------------------- |
| `200` | Streaming started successfully      |
| `400` | Agent disabled or graph unavailable |

---

## POST /v1/extract

Canonical product-facing extraction endpoint (ADR-073). The product repo calls this directly whenever a user submits a URL or place name to save. `/v1/chat` is conversation-only and does not write to `user_places`.

**Request:**

```json
{ "raw_input": "https://www.tiktok.com/@user/video/123", "user_id": "user-uuid" }
```

| Field       | Type     | Required | Description                                                          |
| ----------- | -------- | -------- | -------------------------------------------------------------------- |
| `raw_input` | `string` | yes      | URL (TikTok / Instagram / YouTube / Google Maps list) or place name  |
| `user_id`   | `string` | yes      | The submitting user's ID. Used as the `user_places` owner            |

**Response (200):** `ExtractPlaceResponse`:

```json
{
  "status": "completed",
  "results": [
    {
      "place": { /* PlaceCore */ },
      "confidence": 0.82
    }
  ],
  "raw_input": "https://www.tiktok.com/@user/video/123",
  "request_id": "9f1c…",
  "failure_reason": null,
  "failure_message": null
}
```

| Field             | Type                                            | Notes                                                                                  |
| ----------------- | ----------------------------------------------- | -------------------------------------------------------------------------------------- |
| `status`          | `"pending" \| "completed" \| "failed"`          | Envelope-level only. `results` is non-empty **iff** `status == "completed"`             |
| `results`         | `ExtractPlaceItem[]`                            | `{ place: PlaceCore, confidence: float (0–1) }`. **No per-item `status`** — ADR-071 saves every picker candidate with `approved=False`; the user curates later in the product UI. **No `evidence`** — ADR-093 moved the audit trail to an object-storage ledger so it no longer rides the response |
| `raw_input`       | `string \| null`                                | The original user-supplied string, verbatim                                            |
| `request_id`      | `string \| null`                                | Correlation id                                                                          |
| `failure_reason`  | `string \| null`                                | Populated only when `status == "failed"` (e.g. `unsupported_url`)                       |
| `failure_message` | `string \| null`                                | Human-readable diagnostic, only when `status == "failed"`                               |

ADR-081: the extract response is unchanged. The name the place was shown as in the source post (e.g. a TikTok card title "Mirror Temple", resolver-cleaned of list numbering) is **not** returned here — it is persisted per save on `user_places.source_label` and surfaced when the user's saved places are read. Independently, a confidently-matched source label is added to the shared `place.place_name_aliases` (which feeds search); low-confidence labels stay per-user-only and never enter shared search.

ADR-074: results are cached by canonical URL — a repeat submission of the same URL by another user skips the pipeline and links the cached places to that user (~50 ms vs ~30 s).

**Latency profile:** text → <1 s; caption-only URL → 2–5 s; video needing yt-dlp + Whisper + vision → 30–60 s (synchronous; show a progress indicator).

| Code  | When                                                                                     |
| ----- | ---------------------------------------------------------------------------------------- |
| `200` | Extraction completed or failed — inspect `status` / `failure_reason` in the response     |
| `400` | Malformed request (missing `raw_input` / `user_id`, or `raw_input` exceeds the size cap) |
| `500` | Unhandled pipeline failure                                                               |

---

## DELETE /v1/user/{user_id}/data

Hard-deletes a user's **AI-owned data**. Does NOT delete the user account — that lives in NestJS/Clerk. Called by the product repo's account-deletion flow after it deletes its own `users` / `user_settings` rows.

**Request:**

```
DELETE /v1/user/user_abc/data
DELETE /v1/user/user_abc/data?scope=chat_history
```

**Response (204):** Empty body.

| Param   | Type                                      | Description                                                                                              |
| ------- | ----------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `scope` | repeated `DataScope` (`all` \| `chat_history`) | Selects what to delete. Omit to wipe everything (default). Unknown values → 422. A set containing `all` collapses to a full wipe. |

**What gets deleted (default / `scope=all`):**

1. `interactions` rows where `user_id = ?` (one DB transaction)
2. `user_memories` rows where `user_id = ?`
3. `taste_model` row for `user_id = ?`
4. `user_places` rows where `user_id = ?` (same transaction as 1–3)
5. LangGraph checkpoint thread for `thread_id = user_id`
6. Any pending taste-regen task in the in-memory debouncer

`scope=chat_history` performs only steps 5–6 (SQL untouched).

> **Scope note:** the shared `places` catalog and its `embeddings` are
> **not** in the sweep — those rows are cross-user place identities, not
> this user's data. Only the per-user `user_places` link rows (the
> user's saves plus the source URLs they personally submitted) are
> user-owned and get wiped. The `recommendations` table and the v1
> `places`/`embeddings` tables were dropped by ADR-078.

**Notes:** idempotent (absent user → still 204); synchronous (sub-second at portfolio volume); hard-delete only; no per-user Redis keys to clean; trusted-upstream auth.

---

## POST /v1/signal

Behavioral signal endpoint (ADR-060, narrowed by ADR-076 to recommendation accept/reject only; ADR-078 made it trusted/un-validated).

**Request:**

```json
{
  "signal_type": "recommendation_accepted",
  "user_id": "<user_id>",
  "recommendation_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "place_core_id": "c0ffee00-1111-2222-3333-444455556666"
}
```

| Field               | Type     | Required | Notes                                                                                                                 |
| ------------------- | -------- | -------- | --------------------------------------------------------------------------------------------------------------------- |
| `signal_type`       | `string` | Yes      | `"recommendation_accepted"` or `"recommendation_rejected"`                                                             |
| `user_id`           | `string` | Yes      | Clerk-issued user ID                                                                                                   |
| `recommendation_id` | `string` | Yes      | **Trusted, not validated.** ADR-078 dropped the `recommendations` table; the id is recorded on the event, never looked up |
| `place_core_id`     | `string` | Yes      | `places.id` of the place (ADR-077; renamed from `place_id` to disambiguate from `user_place_id` / `provider_id`). Trusted, not validated |

**Responses:** `202 { "status": "accepted" }`; `422` on schema errors (including an unknown `signal_type`). **No `404`** — ADR-078 removed the recommendation existence check.

---

## GET /v1/health

Health check. **Request:** none.

**Response (200):**

```json
{ "status": "ok", "name": "kebi", "version": "0.1.0", "db": "connected" }
```

| Field     | Type                            | Notes                                                                          |
| --------- | ------------------------------- | ------------------------------------------------------------------------------ |
| `status`  | `string`                        | Always `"ok"` when reachable                                                   |
| `name`    | `string`                        | App name from `config/app.yaml`                                                |
| `version` | `string`                        | Package version; falls back to `"0.1.0"`                                        |
| `db`      | `"connected" \| "disconnected"` | `SELECT 1` probe result                                                        |

Always HTTP 200 — DB outages surface via `db: "disconnected"`.

---

## API Contract Summary

| Endpoint                        | Purpose                              | NestJS Sends                                                   | kebi Returns                                          |
| ------------------------------- | ------------------------------------ | -------------------------------------------------------------- | ----------------------------------------------------- |
| POST /v1/chat                   | Conversational Q&A (zero-tool agent) | user_id, message, optional location                            | type (`agent`\|`error`), message, data, tool_calls_used (0) |
| POST /v1/chat/stream            | SSE streaming chat                   | Same as POST /v1/chat                                           | reasoning_step frames, message frame, done frame      |
| POST /v1/extract                | Canonical extraction (save a place)  | raw_input, user_id                                             | ExtractPlaceResponse                                  |
| GET /v1/extraction/{request_id} | Poll background extraction (reserved) | request_id (path param)                                        | ExtractPlaceResponse (200) or 404                     |
| POST /v1/signal                 | Recommendation accept/reject         | signal_type, user_id, recommendation_id, place_core_id         | status (202)                                          |
| GET /v1/health                  | Service health check                 | —                                                              | status, db connectivity                               |

---

## Error Handling

| Status  | Meaning                                | Product repo action                                     |
| ------- | -------------------------------------- | ------------------------------------------------------- |
| 200     | Success (including `type="error"`)     | Process response                                        |
| 400     | Bad request (malformed input)          | Log error, return 400 to frontend                       |
| 422     | Validation error                       | Return friendly message to frontend                     |
| 500     | AI service internal error              | Log error, return 503 to frontend with retry suggestion |
| Timeout | Service unreachable                    | Return 503 with "service temporarily unavailable"       |

**Timeout policy:** 30 s HTTP client timeout for all AI calls. `POST /v1/extract` on a cold video URL can take up to ~60 s — size that path's timeout accordingly.

---

## Shared Configuration

**Embedding dimensions:** 1024 (Voyage 4-lite). pgvector columns are owned by this repo's Alembic migrations; NestJS never defines vector columns.

**Database tables FastAPI owns (Alembic-managed; NestJS never writes them):**

- `places` — shared place catalog (renamed from `places_v2`, ADR-079)
- `place_embeddings` — place vectors (renamed from `place_embeddings_v2`, ADR-079)
- `user_places` — per-user saved-place links (`approved` curation flag)
- `taste_model` — per-user taste profile
- `interactions` — append-only behavioral signal log
- `user_memories` — personal facts extracted from chat messages

> Dropped in ADR-078: v1 `places`/`embeddings` and `recommendations`.

---

## General Notes

- All requests include `user_id`; the product repo authenticates and validates it upstream (trusted-upstream model).
- FastAPI owns all AI-generated data in PostgreSQL; NestJS owns product data (users, settings). Neither writes the other's tables.
- The `places` / `place_embeddings` table rename (ADR-079) is a coordinated cross-repo change — deploy kebi and the product repo together.
