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

- Base URL loaded from the `KEBI_BASE_URL` env var
- All endpoints are prefixed with `/v1/`
- Most requests are JSON over HTTP (`Content-Type: application/json`)
- `POST /v1/chat/stream` uses Server-Sent Events (`Content-Type: text/event-stream`) — NestJS must forward the stream to the frontend without buffering

### Service-to-service auth (gateway header contract)

Every protected request **MUST** carry two headers signed by NestJS
after it has verified the Clerk session:

| Header              | Value                                                                                                          |
| ------------------- | -------------------------------------------------------------------------------------------------------------- |
| `X-Gateway-Token`   | The shared secret. Same value as `GATEWAY_SHARED_SECRET` in both repos. Constant-time compared.                |
| `X-Gateway-User-Id` | The Clerk subject NestJS just verified (e.g. `user_2pZ1A8KqxYbzABC123…`). Pattern `^user_[A-Za-z0-9]{20,40}$`. |

`user_id` is **no longer a body field** on any request — kebi reads
the verified subject from the header. A missing or wrong token → 401;
malformed user id → 400. The public health probe (`GET /v1/health`)
is the only route that bypasses this check.

Both repos must hold the same `GATEWAY_SHARED_SECRET` byte-for-byte;
rotate by setting the new value on both sides during the same deploy.

### Per-user rate limits

Per-user buckets enforced via slowapi. Excess → HTTP 429. Buckets are
keyed by the verified `X-Gateway-User-Id`.

| Endpoint                    | Bucket      |
| --------------------------- | ----------- |
| POST /v1/chat               | 30 / minute |
| POST /v1/chat/stream        | 30 / minute |
| POST /v1/extract            | 10 / minute |
| GET /v1/user/library        | 60 / minute |
| PATCH /v1/user/places/{id}  | 60 / minute |
| DELETE /v1/user/places/{id} | 60 / minute |
| POST /v1/signal             | 60 / minute |
| DELETE /v1/user/data        | 3 / hour    |

### Request-ID correlation

Every response (success or error) carries an `X-Request-Id` header
with a uuid4 hex. Error response bodies include the same id under
`request_id` so support / oncall can correlate without raw exception
text leaving the server.

---

## Shared Types

### `PlaceCore`

The canonical place shape (ADR-070, ADR-077; the catalog table is
`places` since ADR-079). Identity + static catalog fields only.
Extraction returns `PlaceCore` — it does **not** populate live fields
(rating, hours, popularity, business_status); those are filled in later
by the catalog read/enrichment path that backs the agent's consult-family
tools (ADR-089, ADR-090, ADR-091). There is no standalone product-facing
endpoint for catalog reads today — saved/discovered/suggested places are
returned inside chat responses as `tool_results`.

```json
{
  "id": "c0ffee00-1111-2222-3333-444455556666",
  "provider_id": "google:ChIJN1t_tDeuEmsRUsoyG83frY4",
  "place_name": "Nara Eatery",
  "place_name_aliases": [{ "value": "Nara", "source": "tiktok" }],
  "categories": ["restaurant"],
  "tags": [
    { "type": "cuisine", "value": "Japanese", "source": "google" },
    { "type": "atmosphere", "value": "casual", "source": "llm" }
  ],
  "location": {
    "lat": 13.778,
    "lng": 100.541,
    "address": "123 Ari Soi 4, Bangkok 10400",
    "neighborhood": "Ari",
    "city": "Bangkok",
    "country": "TH"
  },
  "created_at": "2026-04-12T10:15:00Z",
  "refreshed_at": "2026-05-01T08:00:00Z"
}
```

| Field                | Type                        | Notes                                                                                                                                                                                                                    |
| -------------------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `id`                 | `string \| null`            | Catalog primary key (`places.id`). `null` only for freshly-built, unsaved objects                                                                                                                                        |
| `provider_id`        | `string \| null`            | Namespaced external ID (e.g. `"google:ChIJ…"`)                                                                                                                                                                           |
| `place_name`         | `string`                    | Canonical name (provider-sourced)                                                                                                                                                                                        |
| `place_name_aliases` | `{ value, source }[]`       | Alternative names from non-canonical writers (TikTok caption, user note, LLM)                                                                                                                                            |
| `categories`         | `string[]`                  | `PlaceCategory` enum values, e.g. `"restaurant"`, `"cafe"`, `"bar"`                                                                                                                                                      |
| `tags`               | `{ type, value, source }[]` | `type` ∈ `cuisine \| dietary \| feature \| atmosphere \| service \| price \| accessibility \| time \| season` (or an LLM free-text type); `value` is an enum or free-text; `source` e.g. `"google" \| "llm" \| "tiktok"` |
| `location`           | `LocationContext \| null`   | `{ lat, lng, address, neighborhood, city, country }` — any field may be `null`                                                                                                                                           |
| `created_at`         | `ISO-8601 string \| null`   | Catalog row creation                                                                                                                                                                                                     |
| `refreshed_at`       | `ISO-8601 string \| null`   | Last provider refresh                                                                                                                                                                                                    |

> **Migration note (ADR-070/071):** the legacy v1 `PlaceObject` shape
> (`place_type`, `subcategory`, `attributes{}`, Tier 2/3 enrichment
> fields) is gone. `place_type` → `categories: string[]`; `attributes`
> → `tags: [{type,value,source}]`. The v1 `places`/`embeddings` tables
> were dropped in ADR-078.

---

## POST /v1/chat

Unified conversational entry point (ADR-052, ADR-065). The agent is a
LangGraph turn driven by the `orchestrator` LLM role with a small
**consult-family** of internal tools — `find_saved` (the user's saved
places), `suggest_places` (LLM-named candidates validated against the
place provider), and `discover_places` (direct provider lookup for
utility intents and as a fall-through) — see ADR-089, ADR-090, ADR-091.
Each tool returns a structured `ConsultResult` that is surfaced to the
caller in `data.tool_results` so the product UI can render places
without re-parsing the agent's prose. URL submissions are redirected
to `POST /v1/extract` — the chat path never writes to `user_places`.

**Request:**

```json
{
  "message": "somewhere for cheap dinner near me",
  "location": { "lat": 13.7563, "lng": 100.5018 },
  "movement_profile": {
    "available_modes": ["walking", "transit", "motorbike"],
    "reach": "normal"
  }
}
```

(Plus the `X-Gateway-Token` + `X-Gateway-User-Id` headers — see "Service-to-service auth" above.)

| Field              | Type                         | Required | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ------------------ | ---------------------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `message`          | `string`                     | Yes      | Natural-language message from the user. Max 4000 chars; longer payloads → 422.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `location`         | `{ lat: float, lng: float }` | No       | The user's **actual** location — where they physically are. ADR-083 makes this the anchor for per-turn working-location resolution: the agent resolves the location a turn operates against (a place named in the message, one carried from the conversation, or this actual location as fallback) and reverse-geocodes these coords when they are used. Shape unchanged                                                                                                                                                                                                                                                                                                                                                                                                         |
| `movement_profile` | `MovementProfile \| null`    | No       | The user's mobility capability (ADR-084 + ADR-085) — owned by the product repo's `user_settings`, sent each turn like `location`. `{ available_modes, reach }`. `available_modes` items ∈ `walking \| cycling \| motorbike \| driving \| transit \| rideshare`; list is non-empty and represents modes the user _can_ use (licence, owned vehicles, comfort) — NOT per-city availability. `reach` ∈ `compact \| normal \| far`, default `normal`. Omitted → kebi applies a neutral fallback. The agent resolves an effective mode per turn by pairing this capability with the working location's city + density; an explicit mode word in the message still overrides. It never mutates the profile. A stray `default_mode` key (from a pre-ADR-085 client) is silently ignored |

> `user_id` is **no longer a body field**. kebi reads the caller from
> `X-Gateway-User-Id` after the shared-secret check passes.

**Response:**

```json
{
  "type": "agent",
  "message": "Here are three places nearby that fit…",
  "data": {
    "reasoning_steps": [],
    "tool_results": [
      {
        "tool": "find_saved",
        "tool_call_id": "…",
        "payload": {
          /* ConsultResult */
        }
      }
    ]
  },
  "tool_calls_used": 1
}
```

| Field             | Type             | Notes                                                                                                                                                                                           |
| ----------------- | ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `type`            | `string`         | One of `agent`, `error`. No other values are emitted.                                                                                                                                           |
| `message`         | `string`         | Human-readable response text                                                                                                                                                                    |
| `data`            | `object \| null` | `agent`: `{ "reasoning_steps": ReasoningStep[], "tool_results": ToolResult[] }` (user-visible steps only). `error`: `{ "detail": string }`                                                      |
| `tool_calls_used` | `integer`        | Number of tool calls the agent made this turn (0 if the agent answered without retrieval). Surfaced for rate-limit accounting on the NestJS side and capped at `agent.max_tool_calls` (ADR-091) |

`ReasoningStep` shape:

| Field         | Type                    | Notes                                                                                                                                                                      |
| ------------- | ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `step`        | `string`                | Identifier, e.g. `agent.tool_decision`, `find_saved.summary`, `fallback`                                                                                                   |
| `title`       | `string`                | Bold action line (ADR-103), e.g. `searched nearby`. Short, lowercase, carries the verb. Same string on the `active` and `done` SSE frames                                  |
| `summary`     | `string \| null`        | Result line under the title — plain narration, never repeats the verb (no tool names / internal keys). `null` only on an `active` SSE frame; always set on JSON-path steps |
| `source`      | `"agent" \| "fallback"` | Which node produced it (ADR-075 removed the `"tool"` source)                                                                                                               |
| `visibility`  | `"user" \| "debug"`     | Only `"user"` steps appear in the JSON response; `"debug"` → Langfuse/SSE                                                                                                  |
| `timestamp`   | `ISO-8601 string`       | UTC; when the step was recorded                                                                                                                                            |
| `duration_ms` | `float \| null`         | Node latency; non-null in persisted steps                                                                                                                                  |

> The SSE step-lifecycle fields `id` and `status` (ADR-102) are **not** part of
> this non-stream shape — they appear only on `/v1/chat/stream` frames (below).

`ToolResult` shape: `{ tool: "find_saved" | "suggest_places" | "discover_places", tool_call_id: string, payload: ConsultResult }`. `ConsultResult` carries `candidates` (each with `place`, `source ∈ {saved, suggested, discovered}`, optional namer `reason`), and an `empty_reason` literal when no candidates were produced (e.g. `no_location`, `no_match`).

### `error`

```json
{
  "type": "error",
  "message": "Something went wrong, try again",
  "data": { "detail": "..." }
}
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

**Response:** `Content-Type: text/event-stream`. Frame types, in approximately this order:

```
event: reasoning_step
data: {"id":"find_saved#0","step":"find_saved","title":"searched your saved spots","summary":null,"status":"active","source":"agent","visibility":"user","duration_ms":null}

event: reasoning_step
data: {"id":"find_saved#0","step":"find_saved.summary","title":"searched your saved spots","summary":"2 spots — Wagyu, Beef Tei","status":"done","source":"agent","visibility":"user","duration_ms":420.0}

event: tool_result
data: <ToolResult JSON>

event: message
data: {"content": "<final assistant text>"}

event: done
data: {"tool_calls_used": 1}
```

| Frame            | When emitted                                                                                       | Data shape                                                 |
| ---------------- | -------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| `reasoning_step` | Twice per step over its lifecycle (see below) — the agent, every tool, and the fallback all stream | `ReasoningStep` + stream lifecycle fields (`id`, `status`) |
| `tool_result`    | Once per completed tool call this turn                                                             | `ToolResult` (tool, tool_call_id, payload)                 |
| `message`        | Once, after the graph completes, if there is text                                                  | `{"content": string}`                                      |
| `done`           | Always last — even if no message was produced                                                      | `{"tool_calls_used": integer}`                             |

**Step lifecycle (ADR-102).** Each reasoning step is emitted as **two** `reasoning_step` frames keyed by a stable `id`: an `active` frame when the step starts and a `done` frame when it finishes. The frontend upserts by `id`. On the SSE stream `ReasoningStep` carries two fields beyond the JSON-path shape, and relaxes one:

| Field         | On `active` frame       | On `done` frame               | Notes                                                             |
| ------------- | ----------------------- | ----------------------------- | ----------------------------------------------------------------- |
| `id`          | stable step id          | same `id` as the active frame | e.g. `find_saved#0`, `agent.tool_decision#0`; upsert key          |
| `status`      | `"active"`              | `"done"`                      | lifecycle marker                                                  |
| `title`       | set                     | same string                   | bold action line; known before the result, so present on `active` |
| `summary`     | `null`                  | filled                        | client shows a skeleton while `null`                              |
| `visibility`  | set                     | same value                    | must not change across a step's lifecycle (client keys on `id`)   |
| `duration_ms` | `null`                  | set                           | node latency on completion                                        |
| `source`      | `"agent" \| "fallback"` | same                          | ADR-075 narrowed this; no `"tool"` value                          |

Rules: every `done` frame is preceded by an `active` frame with the same `id`; an interrupted step (e.g. a tool that times out mid-phase) may emit `active` with no `done` (renders as a step left in its skeleton). `visibility:"debug"` steps ride the stream too — the client filters them. There is **no** "step N of M" total: the agent decides tools dynamically, so the client shows a live "step N" and a "N steps · time" meta line on completion, no greyed pending rows.

On the stream `id` is always a non-null string and `status` is always `"active"` or `"done"`. The non-stream `POST /v1/chat` omits both fields entirely (its steps are implicitly complete) — that payload is unchanged from before this feature.

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
{ "raw_input": "https://www.tiktok.com/@user/video/123" }
```

(Plus `X-Gateway-Token` + `X-Gateway-User-Id` headers.)

| Field       | Type     | Required | Description                                                                                                                                                                                                                                    |
| ----------- | -------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `raw_input` | `string` | yes      | URL (TikTok / Instagram / YouTube / Google Maps list) or place name. Max 8000 chars. URLs are matched against an exact-suffix host allowlist; an attacker host like `tiktok.com.evil.tld` is rejected with `failure_reason: "unsupported_url"` |

> `user_id` is sourced from `X-Gateway-User-Id` and used as the
> `user_places` owner — never a body field.

**Response (200):** `ExtractPlaceResponse`:

```json
{
  "status": "completed",
  "results": [
    {
      "place": {
        /* PlaceCore */
      },
      "confidence": 0.82
    }
  ],
  "raw_input": "https://www.tiktok.com/@user/video/123",
  "request_id": "9f1c…",
  "failure_reason": null,
  "failure_message": null
}
```

| Field             | Type                                   | Notes                                                                                                                                                                                                                                                                                              |
| ----------------- | -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `status`          | `"pending" \| "completed" \| "failed"` | Envelope-level only. `results` is non-empty **iff** `status == "completed"`                                                                                                                                                                                                                        |
| `results`         | `ExtractPlaceItem[]`                   | `{ place: PlaceCore, confidence: float (0–1) }`. **No per-item `status`** — ADR-071 saves every picker candidate with `approved=False`; the user curates later in the product UI. **No `evidence`** — ADR-093 moved the audit trail to an object-storage ledger so it no longer rides the response |
| `raw_input`       | `string \| null`                       | The original user-supplied string, verbatim                                                                                                                                                                                                                                                        |
| `request_id`      | `string \| null`                       | Correlation id                                                                                                                                                                                                                                                                                     |
| `failure_reason`  | `string \| null`                       | Populated only when `status == "failed"` (e.g. `unsupported_url`)                                                                                                                                                                                                                                  |
| `failure_message` | `string \| null`                       | Human-readable diagnostic, only when `status == "failed"`                                                                                                                                                                                                                                          |

ADR-081: the extract response is unchanged. The name the place was shown as in the source post (e.g. a TikTok card title "Mirror Temple", resolver-cleaned of list numbering) is **not** returned here — it is persisted per save on `user_places.source_label` and surfaced when the user's saved places are read. Independently, a confidently-matched source label is added to the shared `place.place_name_aliases` (which feeds search); low-confidence labels stay per-user-only and never enter shared search.

ADR-074: results are cached by canonical URL — a repeat submission of the same URL by another user skips the pipeline and links the cached places to that user (~50 ms vs ~30 s).

**Latency profile:** text → <1 s; caption-only URL → 2–5 s; video needing yt-dlp + Whisper + vision → 30–60 s (synchronous; show a progress indicator).

| Code  | When                                                                                     |
| ----- | ---------------------------------------------------------------------------------------- |
| `200` | Extraction completed or failed — inspect `status` / `failure_reason` in the response     |
| `400` | Malformed request (missing `raw_input` / `user_id`, or `raw_input` exceeds the size cap) |
| `500` | Unhandled pipeline failure                                                               |

---

## GET /v1/user/library

The Library screen — a browsable, filterable, paged list of the caller's
saved places (`user_places ⋈ places`). This is the first standalone
product-facing catalog read; saved places were previously reachable only
inside chat `tool_results`. `user_id` is taken from `X-Gateway-User-Id`, so
a caller can only ever read **their own** library.

**Request:** query params only (all optional). Plus the
`X-Gateway-Token` + `X-Gateway-User-Id` headers.

```
GET /v1/user/library
GET /v1/user/library?category=cafe&visited=false&source=tiktok&limit=20
GET /v1/user/library?sort=name&limit=20
GET /v1/user/library?sort=name&limit=20&cursor=<next_cursor-from-prior-response>
```

| Param          | Type                                | Notes                                                                                                            |
| -------------- | ----------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `category`     | repeated `PlaceCategory`            | OR across repeats. `?category=cafe&category=bar`                                                                 |
| `tag`          | repeated `string`                   | Tag **value**; AND across repeats (every value must be present)                                                  |
| `city`         | `string`                            | Case-insensitive match on `place.location.city`                                                                  |
| `country`      | `string`                            | Exact match on `place.location.country`                                                                          |
| `source`       | `PlaceSource`                       | `tiktok \| instagram \| youtube \| google_maps_list \| manual \| kebi`                                           |
| `visited`      | `bool`                              | Filter on the user's visited flag                                                                                |
| `liked`        | `bool`                              | Filter on the user's like flag                                                                                   |
| `approved`     | `bool`                              | Curation flag (ADR-071). **Omitted → every save is returned regardless of `approved`**                           |
| `saved_after`  | ISO-8601                            | Saves on/after this instant                                                                                      |
| `saved_before` | ISO-8601                            | Saves on/before this instant                                                                                     |
| `sort`         | `recent \| name` (default `recent`) | The screen's recent ↔ A–Z toggle. `recent` = newest-saved first; `name` = case-insensitive A–Z                   |
| `limit`        | `int` (1–100, default 50)           | Max places per page. Out-of-range → 422                                                                          |
| `cursor`       | `string`                            | Opaque cursor from a prior response's `next_cursor`. Omit for the first page. Malformed or sort-mismatched → 400 |

Filters combine with **AND**. Default order is newest-first (`saved_at`
descending); `sort=name` switches to case-insensitive alphabetical. A
`cursor` is bound to the `sort` it was issued under — replaying it under a
different `sort` is a **400**, so flipping the toggle restarts paging from
the first page (drop the `cursor`). Keep `sort` fixed across a paging run.

**Response (200):** `LibraryResponse`

```json
{
  "places": [
    {
      "place": {
        /* PlaceCore */
      },
      "user_data": {
        "user_place_id": "9b1c…",
        "place_id": "c0ffee00-1111-2222-3333-444455556666",
        "approved": false,
        "visited": false,
        "liked": null,
        "note": null,
        "source": "tiktok",
        "source_ref": "https://www.tiktok.com/@user/video/123",
        "source_label": "Mirror Temple",
        "saved_at": "2026-05-01T08:00:00Z",
        "visited_at": null
      }
    }
  ],
  "next_cursor": "eyJ0cyI6…",
  "total": 42
}
```

| Field         | Type               | Notes                                                                                                                                                                               |
| ------------- | ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `places`      | `SavedPlaceView[]` | `{ place: PlaceCore, user_data: UserPlace }`. `place` carries catalog fields only — no live rating/hours (same as extraction). `user_data` is this user's relationship to the place |
| `next_cursor` | `string \| null`   | Opaque keyset cursor. Pass it back as `?cursor=` for the next page. **`null` on the last page**                                                                                     |
| `total`       | `integer`          | The caller's **grand total** of saved places — the whole stash, **independent of the request's filters and pagination** (drives the screen's hero count). Same on every page        |

`user_data` (`UserPlace`) fields: `user_place_id`, `place_id`, `approved`,
`visited`, `liked` (tri-state, may be `null`), `note`, `source`,
`source_ref` (origin URL; `null` for `manual`/`kebi`), `source_label` (the
name the place was shown as in the source post, ADR-081; `null` when it
matched the canonical name), `saved_at`, `visited_at`. `user_id` is **not**
echoed — the caller already knows who they are.

**Empty state:** a user with no saves (or no matches) returns
`{ "places": [], "next_cursor": null, "total": 0 }` — the empty-state UI is
the product's concern; the shape is guaranteed. (`total` is `0` only for a
user with no saves at all; a filtered page that matches nothing still
reports the unfiltered grand total.)

**Paging:** keyset (cursor) pagination, not offset — stable under new
saves (no skipped/duplicated rows at page boundaries) and fast at any
depth. The cursor anchors on the active sort's key plus `user_place_id`
(`saved_at` for `recent`, the case-folded place name for `name`) and
records which sort minted it; clients treat it as opaque and stop when
`next_cursor` is `null`.

| Code  | When                                                                |
| ----- | ------------------------------------------------------------------- |
| `200` | Success (including the empty library)                               |
| `400` | Malformed `cursor`, or a `cursor` replayed under a different `sort` |
| `422` | Unknown query param, bad enum value, or `limit` out of 1–100        |

---

## PATCH /v1/user/places/{user_place_id}

Update one saved place's **user-state** — the Library pills and menu actions
(been-there / liked / approved / note). `user_id` is taken from
`X-Gateway-User-Id`, so a caller can only ever mutate **their own** save;
ownership is enforced in the update itself (matched on
`(user_place_id, user_id)`).

**Request:** a partial JSON body — only the fields that changed. Plus the
`X-Gateway-Token` + `X-Gateway-User-Id` headers.

```
PATCH /v1/user/places/{user_place_id}
```

```json
{ "visited": true }
```

| Field      | Type             | Notes                                        |
| ---------- | ---------------- | -------------------------------------------- |
| `visited`  | `bool`           | Been-there flag                              |
| `liked`    | `bool \| null`   | Tri-state like. `null` returns it to neutral |
| `approved` | `bool`           | Curation flag                                |
| `note`     | `string \| null` | Free-text note. `null` clears it             |

**Partial semantics:** omitted ≠ null. An **omitted** field is left
untouched; an **explicit `null`** clears it (un-like to neutral, erase a
note). An **empty body** (`{}` or all fields omitted) is rejected with
**422** — a no-op patch is a client mistake. Unknown fields → 422. There is
no server-side `visited_at` stamping; only the flags/note above change.

**Response (200):** `LibraryUserData` — the full updated user-state, the
same shape as `user_data` in the library response (every `UserPlace` field
**except `user_id`**, which is never echoed). Returning the whole object
lets the client replace its local row wholesale.

```json
{
  "user_place_id": "9b1c…",
  "place_id": "c0ffee00-1111-2222-3333-444455556666",
  "approved": false,
  "visited": true,
  "liked": null,
  "note": null,
  "source": "tiktok",
  "source_ref": "https://www.tiktok.com/@user/video/123",
  "source_label": "Mirror Temple",
  "saved_at": "2026-05-01T08:00:00Z",
  "visited_at": null
}
```

| Code  | When                                                                                                                                  |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `200` | Updated — returns the new user-state                                                                                                  |
| `404` | No such save **or** it belongs to another user (`detail: saved_place_not_found`) — the two are indistinguishable, so it leaks nothing |
| `422` | Empty body, unknown field, or bad value type                                                                                          |

---

## DELETE /v1/user/places/{user_place_id}

Remove one saved place from the caller's library (swipe-to-delete / remove).
Hard-deletes the single `user_places` row; the shared catalog place and its
embeddings stay (cross-user data). `user_id` is taken from
`X-Gateway-User-Id` and enforced in the delete (matched on
`(user_place_id, user_id)`), so a caller can only ever delete **their own**
save. The taste model is not recomputed on a single removal.

**Request:** no body. The `X-Gateway-Token` + `X-Gateway-User-Id` headers.

```
DELETE /v1/user/places/{user_place_id}
```

**Response:** `204 No Content` on success (empty body).

| Code  | When                                                                                                                                                                            |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `204` | The caller's save was removed                                                                                                                                                   |
| `404` | No such save **or** it belongs to another user (`detail: saved_place_not_found`) — indistinguishable, so it leaks nothing. A repeat delete of the same id therefore returns 404 |

---

## DELETE /v1/user/data

Hard-deletes a user's **AI-owned data**. Does NOT delete the user account — that lives in NestJS/Clerk. Called by the product repo's account-deletion flow after it deletes its own `users` / `user_settings` rows.

The path no longer carries the `user_id` segment — the target user is
the one identified by `X-Gateway-User-Id`. This guarantees a caller
can only ever wipe their own data, never another user's.

**Request:**

```
DELETE /v1/user/data
DELETE /v1/user/data?scope=chat_history
```

(Plus `X-Gateway-Token` + `X-Gateway-User-Id` headers.)

**Response (204):** Empty body.

| Param   | Type                                           | Description                                                                                                                       |
| ------- | ---------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
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
  "recommendation_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "place_core_id": "c0ffee00-1111-2222-3333-444455556666"
}
```

(Plus `X-Gateway-Token` + `X-Gateway-User-Id` headers.)

| Field               | Type     | Required | Notes                                                                                                                                    |
| ------------------- | -------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `signal_type`       | `string` | Yes      | `"recommendation_accepted"` or `"recommendation_rejected"`                                                                               |
| `recommendation_id` | `string` | Yes      | **Trusted, not validated.** ADR-078 dropped the `recommendations` table; the id is recorded on the event, never looked up                |
| `place_core_id`     | `string` | Yes      | `places.id` of the place (ADR-077; renamed from `place_id` to disambiguate from `user_place_id` / `provider_id`). Trusted, not validated |

> `user_id` is sourced from `X-Gateway-User-Id`, not the body. A caller
> can only register signals for themselves — never poison another
> user's taste profile.

**Responses:** `202 { "status": "accepted" }`; `422` on schema errors (including an unknown `signal_type`). **No `404`** — ADR-078 removed the recommendation existence check.

---

## GET /v1/health

Health check. **Request:** none.

**Response (200):**

```json
{ "status": "ok", "name": "kebi", "version": "0.1.0", "db": "connected" }
```

| Field     | Type                            | Notes                                    |
| --------- | ------------------------------- | ---------------------------------------- |
| `status`  | `string`                        | Always `"ok"` when reachable             |
| `name`    | `string`                        | App name from `config/app.yaml`          |
| `version` | `string`                        | Package version; falls back to `"0.1.0"` |
| `db`      | `"connected" \| "disconnected"` | `SELECT 1` probe result                  |

Always HTTP 200 — DB outages surface via `db: "disconnected"`.

---

## API Contract Summary

All protected calls additionally send the `X-Gateway-Token` + `X-Gateway-User-Id` headers (see "Service-to-service auth").

| Endpoint                    | Purpose                                    | NestJS Sends (body)                                          | kebi Returns                                                                             |
| --------------------------- | ------------------------------------------ | ------------------------------------------------------------ | ---------------------------------------------------------------------------------------- |
| POST /v1/chat               | Conversational turn (consult-family agent) | message, optional location, movement_profile                 | type (`agent`\|`error`), message, data (reasoning_steps + tool_results), tool_calls_used |
| POST /v1/chat/stream        | SSE streaming chat                         | Same as POST /v1/chat                                        | reasoning_step + tool_result + message + done frames                                     |
| POST /v1/extract            | Canonical extraction (save a place)        | raw_input                                                    | ExtractPlaceResponse                                                                     |
| GET /v1/user/library        | Browse the user's saved places (Library)   | — (optional filter + `sort` + `limit`/`cursor` query params) | LibraryResponse (`places: SavedPlaceView[]`, `next_cursor`, `total`)                     |
| PATCH /v1/user/places/{id}  | Update a save's user-state (pills/menu)    | partial body: `visited`/`liked`/`approved`/`note`            | LibraryUserData (updated user-state; `200`/`404`)                                        |
| DELETE /v1/user/places/{id} | Remove one saved place from the library    | — (path param only)                                          | 204 No Content (`404` if absent/not owned)                                               |
| DELETE /v1/user/data        | Account-deletion sweep of AI data          | — (optional `scope` query param)                             | 204 No Content                                                                           |
| POST /v1/signal             | Recommendation accept/reject               | signal_type, recommendation_id, place_core_id                | status (202)                                                                             |
| GET /v1/health              | Service health check (unauthenticated)     | —                                                            | status, db connectivity                                                                  |

---

## Error Handling

| Status  | Meaning                            | Product repo action                                     |
| ------- | ---------------------------------- | ------------------------------------------------------- |
| 200     | Success (including `type="error"`) | Process response                                        |
| 400     | Bad request (malformed input)      | Log error, return 400 to frontend                       |
| 422     | Validation error                   | Return friendly message to frontend                     |
| 500     | AI service internal error          | Log error, return 503 to frontend with retry suggestion |
| Timeout | Service unreachable                | Return 503 with "service temporarily unavailable"       |

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

- All protected requests carry `X-Gateway-Token` (shared HMAC secret) and `X-Gateway-User-Id` (verified Clerk subject). kebi never sees Clerk tokens directly — it trusts the gateway iff the shared secret validates and the user_id matches the expected pattern.
- FastAPI owns all AI-generated data in PostgreSQL; NestJS owns product data (users, settings). Neither writes the other's tables.
- The `places` / `place_embeddings` table rename (ADR-079) is a coordinated cross-repo change — deploy kebi and the product repo together.
- The gateway-auth contract is also a coordinated change — both repos must hold the same `GATEWAY_SHARED_SECRET` and ship together. Rotating the secret means setting the new value in both deploys.
