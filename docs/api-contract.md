# API Contract — product repo ↔ kebi

Source of truth: **this repo (kebi)** — `docs/api-contract.md`. kebi is the server that implements these endpoints and every contract change is driven by an ADR here (`docs/decisions.md`), so the canonical spec lives with the implementation. The product repo (`kebi-app`) keeps a pointer to this file; edit the contract here, then coordinate the client change in the product repo.

This document defines the HTTP contract between the product repo (services/api) and the AI service (kebi). The product repo is the client. The AI repo is the server.

All requests come from NestJS after auth verification. kebi never receives requests directly from the frontend.

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

### Plan-tier entitlements (gateway header contract) — ADR-112

NestJS owns plans/billing; kebi owns no users or plans. The gateway forwards
the caller's **capabilities** (never the plan name) as headers on the same
trusted channel as the identity. kebi enforces them. Repricing or renaming a
tier is a gateway-only change — kebi never sees it.

| Header                              | Value          | Missing →             | Gates                                                       |
| ----------------------------------- | -------------- | --------------------- | ----------------------------------------------------------- |
| `X-Gateway-Taste-Enabled`           | `true`/`false` | `false` (fail closed) | Taste-model personalization on `/v1/chat`                   |
| `X-Gateway-Discovery-Enabled`       | `true`/`false` | `false` (fail closed) | `suggest_places` + `discover_places` agent tools            |
| `X-Gateway-Save-Limit`              | integer        | absent = unlimited    | Max saved places (`/v1/extract`, `/v1/user/places`)         |
| `X-Gateway-Consults-Per-Day`        | integer        | absent = unlimited    | Daily consult quota on `/v1/chat`(+`/stream`)               |
| `X-Gateway-Advanced-Models-Enabled` | `true`/`false` | `false` (fail closed) | Higher-quality orchestrator model on consults               |
| `X-Gateway-Can-Curate`              | `true`/`false` | `false` (fail closed) | Push curated-expert knowledge (`POST /v1/knowledge/curate`) |

Asymmetry by design (ADR-112): the boolean feature flags **fail closed** (a
missing header denies the paid feature); the numeric limits **fail open** to
unlimited (a missing header means no cap), so kebi never has to hard-code the
free-tier numbers. The gateway is expected to always send the limits for
capped tiers.

Enforcement outcomes the gateway should map to an upgrade prompt:

- **Consult quota** — `POST /v1/chat` returns `200` with `type:"error"`,
  `data.reason:"daily_limit_reached"`. `POST /v1/chat/stream` emits an `error`
  frame `{ "detail": "daily_limit_reached" }` then `done`. (Checked at entry —
  a maxed user spends no model cost.)
- **Save cap (button)** — `POST /v1/user/places` returns `403`
  `{ "detail": "save_limit_reached" }`. A re-tap on an already-saved place is
  idempotent and never counts against the cap, so it succeeds even at the limit.
- **Save cap (extraction)** — `POST /v1/extract` returns its terminal envelope
  with `status:"failed"`, `failure_reason:"save_limit_reached"`. Checked
  **before** the pipeline runs, so a user with a full library spends no
  extraction work; a user with room keeps the whole in-flight result.

### Per-user rate limits

Per-user buckets enforced via slowapi. Excess → HTTP 429. Buckets are
keyed by the verified `X-Gateway-User-Id`.

| Endpoint                    | Bucket      |
| --------------------------- | ----------- |
| POST /v1/chat               | 30 / minute |
| POST /v1/chat/stream        | 30 / minute |
| POST /v1/extract            | 10 / minute |
| GET /v1/home                | 30 / minute |
| GET /v1/user/intents        | 60 / minute |
| GET /v1/user/library        | 60 / minute |
| POST /v1/user/places        | 60 / minute |
| POST /v1/knowledge/curate   | 30 / minute |
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
`places` since ADR-079). This is the **complete** place shape the
service returns, everywhere a place appears. Live provider fields
(rating, opening hours, phone, website, popularity, business status)
are **not part of the contract and are not coming later** — the service
stopped requesting them from Google entirely (ADR-118); clients should
not reserve UI for them. Tags carry provenance (`source`:
`"google" | "llm" | ...`): categories and cuisine/dietary tags are
provider-attested, experiential tags (service, feature, price,
atmosphere) come from kebi's knowledge layer and **accumulate over
time** — a freshly discovered place may initially carry only
categories + cuisine tags and densify as content flows through
extraction. Saved places are read via `GET /v1/user/library`;
discovered and suggested places are returned inside chat responses as
`tool_results`.

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
  "icon": "🍜",
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

| Field                | Type                        | Notes                                                                                                                                                                                                                                               |
| -------------------- | --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`                 | `string \| null`            | Catalog primary key (`places.id`). `null` only for freshly-built, unsaved objects                                                                                                                                                                   |
| `provider_id`        | `string \| null`            | Namespaced external ID (e.g. `"google:ChIJ…"`)                                                                                                                                                                                                      |
| `place_name`         | `string`                    | Canonical name (provider-sourced)                                                                                                                                                                                                                   |
| `place_name_aliases` | `{ value, source }[]`       | Alternative names from non-canonical writers (TikTok caption, user note, LLM)                                                                                                                                                                       |
| `categories`         | `string[]`                  | `PlaceCategory` enum values, e.g. `"restaurant"`, `"cafe"`, `"bar"`                                                                                                                                                                                 |
| `tags`               | `{ type, value, source }[]` | `type` ∈ `cuisine \| dietary \| feature \| atmosphere \| service \| price \| accessibility \| time \| season` (or an LLM free-text type); `value` is an enum or free-text; `source` e.g. `"google" \| "llm" \| "tiktok"`                            |
| `icon`               | `string \| null`            | Single emoji for the place's identity (🗼, ⛲, 🌴), LLM-picked where an LLM already sees the place (ADR-117). **Nullable by design** — LLM-less paths (provider discovery) leave it `null`; the client falls back to its own category→emoji mapping |
| `location`           | `LocationContext \| null`   | `{ lat, lng, address, neighborhood, city, country }` — any field may be `null`                                                                                                                                                                      |
| `created_at`         | `ISO-8601 string \| null`   | Catalog row creation                                                                                                                                                                                                                                |
| `refreshed_at`       | `ISO-8601 string \| null`   | Last provider refresh                                                                                                                                                                                                                               |

---

## POST /v1/chat

Unified conversational entry point (ADR-052, ADR-065). The agent is a
LangGraph turn driven by the `orchestrator` LLM role with a small
**consult-family** of internal tools — `find_saved` (the user's saved
places), `suggest_places` (LLM-named candidates validated against the
place provider), `discover_places` (direct provider lookup for
utility intents and as a fall-through) — see ADR-089, ADR-090, ADR-091 —
`suggest_areas` (areas the agent named, verified and placed; ADR-142),
and `research` (insider answers from the knowledge layer's claims
store, ADR-129: what to order, local tricks, fees, safety, timing).
Each tool returns a structured payload that is surfaced to the
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

`ToolResult` shape: `{ tool: "find_saved" | "suggest_places" | "discover_places" | "suggest_areas" | "research", tool_call_id: string, payload: ConsultResult | ResearchResult }`. The `payload` is a union **discriminated by `tool`**: the place tools and `suggest_areas` carry a `ConsultResult`, `research` carries a `ResearchResult` (additive — a client that doesn't render research payloads still gets the agent's prose answer).

`ConsultResult` carries `candidates`, an `empty_reason` literal when no candidates were produced (e.g. `no_location`, `no_match`), and a `recommendation_id` (a per-recommendation id minted by kebi). The client echoes `recommendation_id` back when the user accepts/rejects (`POST /v1/signal`) or saves (`POST /v1/user/places`) a candidate, so the signal attributes to that recommendation.

**A candidate is a venue or an area (ADR-142).** Every candidate carries:

| Field             | Type                          | Notes                                                                                                                                                       |
| ----------------- | ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`              | `string`                      | This item's handle **within this answer**. Not a durable identity — it exists so a follow-up can say "take that one out" and echo an id back                  |
| `kind`            | `"venue" \| "area"`           | Decides the rendering. `venue` → a pin; `area` → a shaded extent, **never a pin**                                                                            |
| `place`           | `PlaceCore \| null`           | Set when `kind == "venue"`, `null` otherwise                                                                                                                |
| `area`            | `AreaSummary \| null`         | Set when `kind == "area"`, `null` otherwise. `{ entity_key, name, kind, lat, lng, extent, parent_key, notes[] }`                                             |
| `extent`          | `[number,number,number,number] \| null` | `[min_lat, max_lat, min_lng, max_lng]`. `null` for a venue, and **also null for an area whose provider geometry could not be trusted** — render a point then |
| `anchor_area_key` | `string \| null`              | Which named area this candidate was found *at*, when the search was anchored on areas. Lets the client group "in Hoi An: …, in Hue: …". `null` on a near-me turn |
| `source`          | `"saved" \| "suggested" \| "discovered"` | Which corpus it came from                                                                                                            |
| `reason`          | `string \| null`              | The namer's one-line rationale, when there is one                                                                                                           |

An **area is not savable as a venue**: it has no `place`, so no `place_core_id`, so `POST /v1/user/places` does not apply to it. Keeping an area is `POST /v1/signal` with `signal_type: "area_saved"` (below), which trains taste and writes no row. This is what makes a mountain pass keepable without becoming a library venue.

`AreaSummary.notes` are the knowledge layer's claims about that area, same shape as the Library's `claims` (`{ id, text, tags, source, agree_count, disagree_count }`).

`ResearchResult` (ADR-129) carries `entity_name` + `entity_key` (the resolved area the notes are about), `notes` — each `{ id, text, tags, source, confidence, agree_count, disagree_count }`, where `id` is the underlying claim's stable id (ADR-128), `tags` are claim-vocabulary values, and `source` is the coarse origin label `community | expert | kebi` (raw provenance never crosses the wire) — plus, when empty, an `empty_reason ∈ { unresolved, ambiguous, no_claims, no_topic_match }` and a `clarification` string. Research results are knowledge, not place candidates: they carry no `recommendation_id` and nothing in them is save/signal-able. Note for the home surface: a turn whose only tool result is `research` does **not** count as a place-surfacing turn for the `GET /v1/user/intents` recall list (ADR-110 semantics preserved).

### `error`

```json
{
  "type": "error",
  "message": "Something went wrong, try again",
  "data": { "detail": "..." }
}
```

`data.detail` is an internal string for logs — safe to ignore in the UI. All downstream exceptions are caught and surfaced as `type="error"` with **HTTP 200** (not 5xx).

When the caller's daily consult quota (`X-Gateway-Consults-Per-Day`) is exhausted, the same `type="error"` shape carries a structured reason instead: `data.reason:"daily_limit_reached"` (HTTP 200). The agent does not run. Map it to an upgrade prompt — see "Plan-tier entitlements" above.

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

If the daily consult quota is exhausted, the stream opens (HTTP 200) and immediately emits a terminal `error` frame with `{"detail": "daily_limit_reached"}` followed by `done` — no reasoning or message frames. Map it to an upgrade prompt.

| Code  | When                                |
| ----- | ----------------------------------- |
| `200` | Streaming started successfully      |
| `400` | Agent disabled or graph unavailable |

---

## GET /v1/home

The home screen's opening surface (ADR-111): a short, context-aware
**greeting** plus a few tappable suggestion **chips**. Generated from the
caller's taste signal and the client-supplied local context. Each chip's
`text` is a pre-written intent — the client re-submits it to `POST /v1/chat`
on tap, so a chip is a first message, not a separate action. `user_id` is
taken from `X-Gateway-User-Id`.

The payload is Redis-cached per coarse context bucket (so most opens are a
cache hit; a taste regeneration refreshes it) and **fails open** — any
generation/cache/geocode error returns a neutral greeting + generic chips, so
the screen always renders and the call always returns `200`.

**Request:** query params only (all optional). Plus the
`X-Gateway-Token` + `X-Gateway-User-Id` headers.

```
GET /v1/home
GET /v1/home?city=shimokitazawa&local_time=2026-06-28T21:41:00&weather=clear
GET /v1/home?lat=35.6615&lng=139.6680&local_time=2026-06-28T21:41:00
```

| Param        | Type               | Notes                                                                                                                  |
| ------------ | ------------------ | ---------------------------------------------------------------------------------------------------------------------- |
| `lat`        | `float` (−90–90)   | Device latitude. Used only to reverse-geocode a city name when `city` is absent — the server never originates location |
| `lng`        | `float` (−180–180) | Device longitude (paired with `lat`)                                                                                   |
| `city`       | `string`           | Client-supplied city name; when present, skips reverse-geocoding                                                       |
| `local_time` | ISO-8601           | Device local time. Drives the daypart (morning/afternoon/evening/late_night) — the client's timezone is canonical      |
| `weather`    | `string`           | Coarse free-text hint (e.g. `clear`, `rain`); folded into a small band server-side. Omit when unknown                  |

**Response (200):** `HomeResponse`

```json
{
  "greeting": "it's late, drunk food?",
  "chips": [
    { "text": "ramen, no line" },
    { "text": "drinks somewhere chill" },
    { "text": "dessert, walking distance" },
    { "text": "surprise me" }
  ]
}
```

| Field      | Type         | Notes                                                                                                 |
| ---------- | ------------ | ----------------------------------------------------------------------------------------------------- |
| `greeting` | `string`     | Short context-aware line                                                                              |
| `chips`    | `{ text }[]` | 3–4 suggestion chips. `text` is both the display label and the intent re-submitted to `POST /v1/chat` |

The chips emit **no** taste signal on their own — only an actual chat turn,
save, or accept/reject trains taste. There is no chip-confirmation endpoint.

| Code  | When                                                 |
| ----- | ---------------------------------------------------- |
| `200` | Always on success — including the fail-open fallback |
| `422` | Unknown query param, or `lat`/`lng` out of range     |

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
  "failure_message": null,
  "noted_interests": [
    {
      "name": "Ha Giang Loop",
      "message": "'Ha Giang Loop' looks like a route or region rather than a single place — I've noted it as a travel interest instead of saving it."
    }
  ]
}
```

| Field             | Type                                   | Notes                                                                                                                                                                                                                                                                                              |
| ----------------- | -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `status`          | `"pending" \| "completed" \| "failed"` | Envelope-level only. `status == "completed"` requires at least one of `results` / `noted_interests`; the other statuses carry neither                                                                                                                                                              |
| `results`         | `ExtractPlaceItem[]`                   | `{ place: PlaceCore, confidence: float (0–1) }`. **No per-item `status`** — ADR-071 saves every picker candidate with `approved=False`; the user curates later in the product UI. **No `evidence`** — ADR-093 moved the audit trail to an object-storage ledger so it no longer rides the response |
| `raw_input`       | `string \| null`                       | The original user-supplied string, verbatim                                                                                                                                                                                                                                                        |
| `request_id`      | `string \| null`                       | Correlation id                                                                                                                                                                                                                                                                                     |
| `failure_reason`  | `string \| null`                       | Populated only when `status == "failed"`. One of `unsupported_url`, `empty_input`, `no_candidates`, `all_below_threshold`, `candidate_limit_exceeded`, `pipeline_error`, `save_limit_reached`                                                                                                      |
| `failure_message` | `string \| null`                       | Human-readable diagnostic, only when `status == "failed"`                                                                                                                                                                                                                                          |
| `noted_interests` | `NotedInterest[]`                      | `{ name: string, message: string }`. Non-venue geography the share referenced (a route, region, town, natural feature) — detected, acknowledged, **never saved**. Render `message` (or your own copy off `name`) so the user's action has a visible outcome. May accompany non-empty `results`     |

**New legal outcome:** `status: "completed"` with empty `results` and non-empty `noted_interests` — the share contained only non-venue geography (e.g. a video about a motorbike loop). This is a success, not a failure; nothing was saved and the acknowledgment is the payload. Callers that previously treated `completed` as "results present" must handle this shape.

Note: `noted_interests` does not ride the ADR-074 URL result cache — a repeat submission of an already-cached URL returns the cached `results` without the acknowledgments. Accepted limitation while noted interests are acknowledge-only.

ADR-081: the extract response is unchanged. The name the place was shown as in the source post (e.g. a TikTok card title "Mirror Temple", resolver-cleaned of list numbering) is **not** returned here — it is persisted per save on `user_places.source_label` and surfaced when the user's saved places are read. Independently, a confidently-matched source label is added to the shared `place.place_name_aliases` (which feeds search); low-confidence labels stay per-user-only and never enter shared search.

ADR-074: results are cached by canonical URL — a repeat submission of the same URL by another user skips the pipeline and links the cached places to that user (~50 ms vs ~30 s).

ADR-112: if the caller's `X-Gateway-Save-Limit` is already met, extraction returns `status:"failed"`, `failure_reason:"save_limit_reached"` **before** running the pipeline (no LLM/vision/transcription spend). A user below the limit gets their whole in-flight result even if it nudges them over — the cap is a "you're full, stop" gate, not a per-place counter.

**Latency profile:** text → <1 s; caption-only URL → 2–5 s; video needing yt-dlp + Whisper + vision → 30–60 s (synchronous; show a progress indicator).

| Code  | When                                                                                     |
| ----- | ---------------------------------------------------------------------------------------- |
| `200` | Extraction completed or failed — inspect `status` / `failure_reason` in the response     |
| `400` | Malformed request (missing `raw_input`, or `raw_input` exceeds the size cap) |
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
      },
      "claims": [
        {
          "id": "c0ffee00-aaaa-bbbb-cccc-dddddddddddd",
          "text": "order the omakase — it's off-menu",
          "tags": ["food"],
          "source": "community",
          "from_shared": true,
          "agree_count": 0,
          "disagree_count": 0
        }
      ]
    }
  ],
  "next_cursor": "eyJ0cyI6…",
  "total": 42
}
```

| Field    | Type               | Notes                                                                                                                                                                                                                           |
| -------- | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `places` | `SavedPlaceView[]` | `{ place: PlaceCore, user_data: UserPlace, claims: PlaceNote[] }`. `place` is the complete place shape — live rating/hours don't exist anywhere in the contract (ADR-118). `user_data` is this user's relationship to the place |
| `next_cursor` | `string \| null` | Opaque keyset cursor. Pass it back as `?cursor=` for the next page. **`null` on the last page** |
| `total` | `integer` | The caller's **grand total** of saved places — the whole stash, **independent of the request's filters and pagination** (drives the screen's hero count). Same on every page |

`claims` (`PlaceNote[]`, ADR-127) are the **insider notes** tied to the place
from the knowledge layer — the payoff surface. Each: `id` (the claim's stable
id — use as the list key and, later, the vote target), `text` (the note),
`tags: string[]`, `source` (coarse origin label: `community` = harvested from
shared content, `expert` = curated, `kebi` = the user's own saved-recommendation
reason), `from_shared: bool` — `true` when the note was mined from the very
post the user shared for this save (badge it "from what you shared") — and
`agree_count` / `disagree_count` (`integer`), the claim's corroboration tally.
Both are `0` today and only move once the agree/disagree vote write-path ships;
they are surfaced now so the client can render the counts without a later
contract change. Approved claims only, strongest first, capped. A place with no
claims returns `[]` — no empty section. v1 is place-scoped; city/neighborhood
ambient notes are not yet included.

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

## POST /v1/user/places

Save a place kebi recommended to the caller's library — the consult card's
**"save it"** action. The place already exists in the catalog (it was just
recommended), so this only links it to the caller. `user_id` is taken from
`X-Gateway-User-Id`; a caller can only ever save into **their own** library.

Saving also emits a **positive taste signal** — a _stronger_ one than a
link-share save: its own `saved_recommendation` interaction type, weighted
heavier in the taste evidence and **not** counted toward the discovery-source
distribution (kebi is not a channel the user discovers from).

**Request:** JSON body + the `X-Gateway-Token` + `X-Gateway-User-Id` headers.

```
POST /v1/user/places
```

```json
{
  "place_core_id": "c0ffee00-1111-2222-3333-444455556666",
  "recommendation_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "reason": "great for a quiet date — the back room is candlelit"
}
```

| Field               | Type            | Required | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ------------------- | --------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `place_core_id`     | `string`        | Yes      | `places.id` of the candidate (consult `tool_results → payload.candidates[].place.id`)                                                                                                                                                                                                                                                                                                                                                      |
| `recommendation_id` | `string`        | Yes      | The id kebi minted on that consult result (`tool_results → payload.recommendation_id`)                                                                                                                                                                                                                                                                                                                                                     |
| `reason`            | `string`/`null` | No       | The pick's rationale the card is showing. The reason is **not** persisted server-side, so the client supplies it. On create it is written to the knowledge layer as a **user-scoped `kebi_message` claim** on the place (ADR-127) and surfaces in the Library's `claims` as a `kebi` note — it is **no longer stored on the save as a note** (this amends ADR-114). A re-tap adds nothing (claim-text dedup). Omit or `null` for no reason |

> **ADR-127 note:** the save no longer echoes the reason back as `user_data.note`.
> `user_data.note` is now set only by the user's own edit (`PATCH /v1/user/places/{id}`);
> the reason appears instead in the place's Library `claims` (`source: "kebi"`).
> Clients that sent `note` here must send `reason` and read it from `claims`.

`source` is **not** a field — the server stamps `kebi`. Unknown fields → 422.

**Response (201):** `LibraryUserData` — the created user-state, the same
shape as `user_data` in the library response (every `UserPlace` field
**except `user_id`**).

**Idempotent:** re-tapping save on an already-saved place returns **201**
with the existing save and does **not** re-emit the taste signal — saving
twice never double-trains taste.

| Code  | When                                                                                                                                                     |
| ----- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `201` | Saved (or already saved) — returns the user-state                                                                                                        |
| `403` | `X-Gateway-Save-Limit` already met (`detail: save_limit_reached`) — map to upgrade. A re-tap on an already-saved place is exempt and still returns `201` |
| `404` | `place_core_id` is not in the catalog (`detail: place_not_found`)                                                                                        |
| `422` | Missing `place_core_id`/`recommendation_id`, or an unknown field                                                                                         |

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

## GET /v1/user/intents

The home screen's **"what you wanted"** list (ADR-110): the caller's recent
intent-bearing chat turns, played back verbatim, newest-first. Tapping a row
re-submits its `text` to `POST /v1/chat`. `user_id` is taken from
`X-Gateway-User-Id`, so a caller can only ever read **their own** intents.

Intents are persisted server-side from the chat turn when the turn actually
surfaced places **and** passes a noise gate (minimum word count, a
confirmation/ordinal stoplist, and de-duplication of an immediate repeat) — so
chit-chat and one-word replies never appear here. There is no client write
endpoint; the list is populated as a side effect of `POST /v1/chat`.

**Request:** query params only (all optional). Plus the
`X-Gateway-Token` + `X-Gateway-User-Id` headers.

```
GET /v1/user/intents
GET /v1/user/intents?limit=20
GET /v1/user/intents?limit=20&cursor=<next_cursor-from-prior-response>
```

| Param    | Type                      | Notes                                                                                         |
| -------- | ------------------------- | --------------------------------------------------------------------------------------------- |
| `limit`  | `int` (1–100, default 20) | Max intents per page. Out-of-range → 422                                                      |
| `cursor` | `string`                  | Opaque cursor from a prior response's `next_cursor`. Omit for the first page. Malformed → 400 |

**Response (200):** `IntentsResponse`

```json
{
  "intents": [
    {
      "id": "9f1c2a3b-4d5e-6789-abcd-ef0123456789",
      "text": "coffee, quiet, nowhere i've been",
      "created_at": "2026-06-27T08:42:00Z"
    }
  ],
  "next_cursor": "eyJ0cyI6…"
}
```

| Field         | Type             | Notes                                                                                              |
| ------------- | ---------------- | -------------------------------------------------------------------------------------------------- |
| `intents`     | `IntentItem[]`   | `{ id, text, created_at }`. `text` is the verbatim message; re-submit it to `POST /v1/chat` on tap |
| `next_cursor` | `string \| null` | Opaque keyset cursor. Pass it back as `?cursor=` for the next page. **`null` on the last page**    |

`created_at` is a **raw ISO-8601 instant** — relative phrasing ("yesterday,
8:42") is the client's to render, since only the client knows the user's
timezone. `user_id` is **not** echoed.

**Empty state:** a user with no recalled intents returns
`{ "intents": [], "next_cursor": null }` — the shape is guaranteed.

| Code  | When                                         |
| ----- | -------------------------------------------- |
| `200` | Success (including the empty history)        |
| `400` | Malformed `cursor`                           |
| `422` | Unknown query param, or `limit` out of 1–100 |

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
4. `user_intents` rows where `user_id = ?` (same transaction as 1–3)
5. `user_places` rows where `user_id = ?` (same transaction as 1–4)
6. LangGraph checkpoint thread for `thread_id = user_id`
7. Any pending taste-regen task in the in-memory debouncer

`scope=chat_history` deletes the `user_intents` rows (step 4) + the checkpoint
thread + the debouncer task (steps 6–7) — the recall list is surfaced
conversation history (ADR-110), so "clear chat history" must clear it. The
other SQL tables (saves, memories, taste model) are left untouched.

> **Scope note:** the shared `places` catalog and its `embeddings` are
> **not** in the sweep — those rows are cross-user place identities, not
> this user's data. Only the per-user `user_places` link rows (the
> user's saves plus the source URLs they personally submitted) are
> user-owned and get wiped. `knowledge_claims` are **not** swept —
> neither global claims (cross-user world knowledge) nor the user's own
> `kebi_message` reasons (ADR-127), which are deliberately retained as
> place knowledge rather than erased.

**Notes:** idempotent (absent user → still 204); synchronous (sub-second at portfolio volume); hard-delete only; no per-user Redis keys to clean; trusted-upstream auth.

---

## POST /v1/signal

Behavioral signal endpoint (ADR-060, narrowed by ADR-076 to recommendation accept/reject; ADR-078 made it trusted/un-validated; ADR-142 added the area save).

The body is a **union discriminated by `signal_type`** — each signal declares only the fields it needs.

**Request — recommendation accept/reject:**

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

**Request — keeping an area (ADR-142):**

```json
{
  "signal_type": "area_saved",
  "entity_key": "vn/da-nang/an-thuong",
  "recommendation_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
}
```

| Field               | Type            | Required | Notes                                                                                                                                                    |
| ------------------- | --------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `signal_type`       | `string`        | Yes      | `"area_saved"`                                                                                                                                            |
| `entity_key`        | `string`        | Yes      | The area's `entity_key` from the answer card (`candidates[].area.entity_key`). A key kebi never resolved is a **no-op**, not an error                     |
| `recommendation_id` | `string`/`null` | No       | The answer the area was surfaced in, when the client has it                                                                                                |

> **An area save writes no row.** It records region interest against the
> taste model (the same signal a shared area produces, ADR-135) and stops
> there — no library entry, no venue row, nothing to list or delete. This is
> why an area card's save action is `POST /v1/signal` and not
> `POST /v1/user/places`: it is what lets a user keep "Hai Van Pass" without
> a mountain pass appearing in their library as if it were a restaurant.
> The library stays venue-only.

> `user_id` is sourced from `X-Gateway-User-Id`, not the body. A caller
> can only register signals for themselves — never poison another
> user's taste profile.

**Responses:** `202 { "status": "accepted" }`; `422` on schema errors (including an unknown `signal_type`, or a body missing the fields its `signal_type` requires). **No `404`** — ADR-078 removed the recommendation existence check, and an unresolvable `entity_key` is a silent no-op.

---

## POST /v1/knowledge/curate

Push expert knowledge into the knowledge layer (ADR-121). The caller writes
prose; kebi's LLM structures it into geo-scoped claims (country / city /
neighborhood) and stores them as `curated_expert` — **global** knowledge, not
scoped to the caller. v1 does not curate individual venues.

**Gated:** requires the `X-Gateway-Can-Curate` capability header set to
`true`. Missing/`false` → `403 { "detail": "curation_not_permitted" }` (fail
closed, since these are global writes). `user_id` is taken only from
`X-Gateway-User-Id` and recorded as provenance, never as a claim scope.

(Plus `X-Gateway-Token` + `X-Gateway-User-Id` headers.)

**Request:**

```json
{
  "text": "Jumeirah's beach clubs are pricey but the sunset views are unreal. Dubai nightlife peaks after midnight.",
  "location_hint": { "country_alpha2": "ae", "city": "Dubai" }
}
```

| Field                          | Type     | Notes                                                               |
| ------------------------------ | -------- | ------------------------------------------------------------------- |
| `text`                         | `string` | Required, non-empty. The expert's prose.                            |
| `location_hint`                | `object` | Optional. Fallback geography when a claim's area can't be geocoded. |
| `location_hint.country_alpha2` | `string` | ISO-3166 alpha-2 (e.g. `"ae"`).                                     |
| `location_hint.city`           | `string` | Optional.                                                           |
| `location_hint.neighborhood`   | `string` | Optional.                                                           |

**Response (200):**

```json
{
  "claims_written": 2,
  "claims": [
    {
      "scope": "neighborhood",
      "entity_name": "Jumeirah",
      "claim": "Beach clubs are pricey but the sunset views are exceptional.",
      "tags": ["nightlife", "price", "scenery"]
    },
    {
      "scope": "city",
      "entity_name": "Dubai",
      "claim": "Nightlife peaks after midnight.",
      "tags": ["nightlife"]
    }
  ]
}
```

| Field            | Type       | Notes                                                                                                                                                 |
| ---------------- | ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `claims_written` | `integer`  | Count of **new** rows stored. May be less than the prose implied — dedup collapses re-submissions, and unkeyable or accessibility claims are dropped. |
| `claims`         | `object[]` | The stored claims: `{ scope, entity_name, claim, tags }`. Empty when nothing was stored.                                                              |

Accessibility claims are never stored (an unverified accessibility claim is real-world harm). Harvested (`shared_content`) and curated (`curated_expert`) claims about the same entity merge on the same key and are separable only by their source.

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

| Endpoint                    | Purpose                                    | NestJS Sends (body)                                          | kebi Returns                                                                                                                                 |
| --------------------------- | ------------------------------------------ | ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------- |
| POST /v1/chat               | Conversational turn (consult-family agent) | message, optional location, movement_profile                 | type (`agent`\|`error`), message, data (reasoning_steps + tool_results), tool_calls_used                                                     |
| POST /v1/chat/stream        | SSE streaming chat                         | Same as POST /v1/chat                                        | reasoning_step + tool_result + message + done frames                                                                                         |
| GET /v1/home                | Home greeting + suggestion chips           | — (optional `lat`/`lng`/`city`/`local_time`/`weather` query) | HomeResponse (`greeting`, `chips: { text }[]`); fail-open, always `200`                                                                      |
| GET /v1/user/intents        | "What you wanted" recall list              | — (optional `limit`/`cursor` query params)                   | IntentsResponse (`intents: { id, text, created_at }[]`, `next_cursor`)                                                                       |
| POST /v1/extract            | Canonical extraction (save a place)        | raw_input                                                    | ExtractPlaceResponse                                                                                                                         |
| GET /v1/user/library        | Browse the user's saved places (Library)   | — (optional filter + `sort` + `limit`/`cursor` query params) | LibraryResponse (`places: SavedPlaceView[]`, `next_cursor`, `total`)                                                                         |
| POST /v1/user/places        | Save a recommended place ("save it")       | place_core_id, recommendation_id, optional `reason`          | LibraryUserData (created user-state, `201`; `404` if uncatalogued); emits taste signal + writes `reason` as a `kebi_message` claim (ADR-127) |
| PATCH /v1/user/places/{id}  | Update a save's user-state (pills/menu)    | partial body: `visited`/`liked`/`approved`/`note`            | LibraryUserData (updated user-state; `200`/`404`)                                                                                            |
| DELETE /v1/user/places/{id} | Remove one saved place from the library    | — (path param only)                                          | 204 No Content (`404` if absent/not owned)                                                                                                   |
| DELETE /v1/user/data        | Account-deletion sweep of AI data          | — (optional `scope` query param)                             | 204 No Content                                                                                                                               |
| POST /v1/signal             | Recommendation accept/reject, or area save | signal_type + (recommendation_id, place_core_id) \| entity_key | status (202)                                                                                                                                 |
| GET /v1/health              | Service health check (unauthenticated)     | —                                                            | status, db connectivity                                                                                                                      |

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

- `places` — shared place catalog
- `place_embeddings` — place vectors
- `user_places` — per-user saved-place links (`approved` curation flag)
- `taste_model` — per-user taste profile
- `interactions` — append-only behavioral signal log
- `user_memories` — personal facts extracted from chat messages
- `user_intents` — the home "what you wanted" recall list (ADR-110); intent-bearing chat turns
- `knowledge_claims` — entity-scoped world-knowledge claims (ADR-120)

---

## General Notes

- All protected requests carry `X-Gateway-Token` (shared HMAC secret) and `X-Gateway-User-Id` (verified Clerk subject). kebi never sees Clerk tokens directly — it trusts the gateway iff the shared secret validates and the user_id matches the expected pattern.
- FastAPI owns all AI-generated data in PostgreSQL; NestJS owns product data (users, settings). Neither writes the other's tables.
- The gateway-auth contract is a coordinated change — both repos must hold the same `GATEWAY_SHARED_SECRET` and ship together. Rotating the secret means setting the new value in both deploys.
