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
| `X-Gateway-Can-Curate`              | `true`/`false` | `false` (fail closed) | All `/v1/knowledge/*` curation endpoints: curate, list/retract own claims, entity typeahead |

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
| GET /v1/user/library/areas  | 60 / minute |
| POST /v1/user/places        | 60 / minute |
| POST /v1/knowledge/curate   | 30 / minute |
| GET /v1/knowledge/claims    | 60 / minute |
| DELETE /v1/knowledge/claims/{id} | 60 / minute |
| GET /v1/knowledge/entities  | 120 / minute |
| PATCH /v1/user/places/{id}  | 60 / minute |
| DELETE /v1/user/places/{id} | 60 / minute |
| GET /v1/places/{id}         | 60 / minute |
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
discovered and suggested places are no longer returned inside chat
responses at all (ADR-136) — chat sends text plus `kebi://venue/{id}`
links, and the client fetches the place for the detail screen the link
opens.

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
and `research` (insider answers from the knowledge layer's claims
store, ADR-129: what to order, local tricks, fees, safety, timing).
Tool payloads stay server-side (ADR-136). What the caller renders is
the answer text with entity names already wrapped as markdown links to
`kebi://{kind}/{entity_key}` URIs, plus a flat `entities` list
resolving each link. Three kinds:

| Kind    | URI                          | `entity_key`                              | Tap opens          |
| ------- | ---------------------------- | ----------------------------------------- | ------------------ |
| `venue` | `kebi://venue/{place id}`    | `places.id` — the id `GET /v1/places/{id}` and `POST /v1/user/places` take | The place screen   |
| `area`  | `kebi://area/{token}`        | `{cc}[/{cityId}[/{areaId}]]` — geo-registry id path (ADR-169). **Opaque**: never parse or display segments; `/`-prefix nesting is the only structure a client may rely on | A light area sheet |
| `web`   | `kebi://web/{token}`         | The full page URL the turn's `web_search` read | The page, in a browser |

A `web` entity appears only on turns where `web_search` actually fired
(stored claims keep no URL — ADR-145 — so a claim-based answer never
carries a citation link). The prose names the source domain ("per the
schedule on fifa.com") and that domain mention is the wrapped text;
`name` is the domain, `key` is the raw page URL, and the URI's token is
**base64url (no padding) of the page URL** — no resolve endpoint
exists; the client decodes the token locally and opens the URL. One
entity per domain: the top-ranked page from that domain.

Only entities actually retrieved this turn are ever linked, and only
the first mention of each is wrapped — an unrecognised name stays
plain text. A new tool therefore changes what the agent says, never
what the client draws. URL submissions are redirected
to `POST /v1/extract` — the chat path never writes to `user_places`.

**Request:**

```json
{
  "message": "somewhere for cheap dinner near me",
  "location": { "lat": 13.7563, "lng": 100.5018 },
  "movement_profile": {
    "available_modes": ["walking", "transit", "motorbike"],
    "reach": "normal"
  },
  "user_profile": {
    "call_me": "Saher",
    "home_country": "AE",
    "about": "I'd rather eat where locals eat than anywhere with a queue. I don't drink."
  }
}
```

(Plus the `X-Gateway-Token` + `X-Gateway-User-Id` headers — see "Service-to-service auth" above.)

| Field              | Type                         | Required | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ------------------ | ---------------------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `message`          | `string`                     | Yes      | Natural-language message from the user. Max 4000 chars; longer payloads → 422.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `location`         | `{ lat: float, lng: float }` | No       | The user's **actual** location — where they physically are. ADR-083 makes this the anchor for per-turn working-location resolution: the agent resolves the location a turn operates against (a place named in the message, one carried from the conversation, or this actual location as fallback) and reverse-geocodes these coords when they are used. Shape unchanged                                                                                                                                                                                                                                                                                                                                                                                                         |
| `local_time`       | `string \| null`             | No       | The caller's local wall-clock time, ISO-8601 (`2026-08-10T19:30:00+08:00`). Client-supplied for the same reason `location` is — only the device knows the user's real clock, and a server clock in another timezone answers for the wrong day. Day of week is load-bearing (ADR-138): a claim that Monday is a venue's big night is only usable if kebi knows today is Monday. Omitted → the agent answers without a schedule rather than guessing one. Max 40 chars |
| `movement_profile` | `MovementProfile \| null`    | No       | The user's mobility capability (ADR-084 + ADR-085) — owned by the product repo's `user_settings`, sent each turn like `location`. `{ available_modes, reach }`. `available_modes` items ∈ `walking \| cycling \| motorbike \| driving \| transit \| rideshare`; list is non-empty and represents modes the user _can_ use (licence, owned vehicles, comfort) — NOT per-city availability. `reach` ∈ `compact \| normal \| far`, default `normal`. Omitted → kebi applies a neutral fallback. The agent resolves an effective mode per turn by pairing this capability with the working location's city + density; an explicit mode word in the message still overrides. It never mutates the profile. A stray `default_mode` key (from a pre-ADR-085 client) is silently ignored. `source` ∈ `user \| default`, **default `default`** (ADR-155): it says whether a human ever chose these modes. Only `user` counts as resolved — an unchosen block still supplies its modes, but kebi treats movement as unknown and the agent should ask rather than assert distance. Send `source: "user"` only for rows a user actually set; a config-seeded row is `default`. An unchosen block's modes are **ignored** (ADR-156): kebi substitutes its own deliberately wide fallback (leads with `rideshare`) and lifts an inferred narrow mode, because capping an unknown user at walking range hides places they never learn about. A mode the user states in the message, and a walking-distance request, are never widened. Modes the user states in conversation ("we rented a scooter") outrank this block for the rest of the trip and are cleared when the working location's country changes |
| `user_profile`     | `UserProfile \| null`        | No       | The user's "about me" block (ADR-154) — owned by the product repo's `user_settings`, sent each turn like `movement_profile`. `{ call_me, home_country, about }`, every field optional and nullable. `call_me` is a display name, max 40 chars. `home_country` is ISO 3166-1 alpha-2, case-insensitive on the way in and normalized upper (`ae` → `AE`); names and alpha-3 codes → 422. `about` is free prose, max 300 chars. Whitespace-only `call_me` / `about` are treated as absent. kebi never stores it and never returns it: `about` reaches the model as `trust="low"` data, weighted as a cold-start prior that observed behavior overrides — except a restriction stated in it (diet, religion, allergy), which is read as a hard constraint. `home_country` present → entry/visa questions are answered from a live `web_search` every time and never mined into durable claims |

> `user_id` is **no longer a body field**. kebi reads the caller from
> `X-Gateway-User-Id` after the shared-secret check passes.

**Response:**

```json
{
  "type": "agent",
  "message": "its monday so tonight is [Luigis](kebi://venue/c0ffee00-…) night, their big night in [Canggu](kebi://area/aWQvQ2hJSm9R…)",
  "data": {
    "reasoning_steps": [],
    "entities": [
      {
        "kind": "venue",
        "key": "c0ffee00-…",
        "name": "Luigis",
        "uri": "kebi://venue/c0ffee00-…",
        "icon": "🍕"
      },
      {
        "kind": "area",
        "key": "id/ChIJoQ8Q…/ChIJZZZY…",
        "name": "Canggu",
        "uri": "kebi://area/aWQvQ2hJSm9R…",
        "icon": "🏄"
      }
    ],
    "recommendation_id": "9c1e…"
  },
  "tool_calls_used": 1
}
```

| Field             | Type             | Notes                                                                                                                                                                                           |
| ----------------- | ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `type`            | `string`         | One of `agent`, `error`. No other values are emitted.                                                                                                                                           |
| `message`         | `string`         | Response text. On the `agent` path, entity names are already wrapped as markdown links to `kebi://{kind}/{key}` (ADR-136)                                                                       |
| `data`            | `object \| null` | `agent`: `{ "reasoning_steps": ReasoningStep[], "entities": ChatEntity[], "recommendation_id": string \| null }` (user-visible steps only). `error`: `{ "detail": string }`                       |
| `tool_calls_used` | `integer`        | Number of tool calls the agent made this turn (0 if the agent answered without retrieval). Surfaced for rate-limit accounting on the NestJS side and capped at `agent.max_tool_calls` (ADR-091) |

`ChatEntity` shape — one per link in `message`, in the order they appear:

| Field  | Type                  | Notes                                                                                    |
| ------ | --------------------- | ---------------------------------------------------------------------------------------- |
| `kind` | `"venue" \| "area" \| "web"` | Which detail surface the tap opens. Unknown kinds must degrade to plain text, never crash — the vocabulary can grow |
| `key`  | `string`              | `places.id` for a venue; the canonical geo key for an area (opaque registry id path — round-trip it, never parse it); the raw page URL for a web source |
| `name` | `string`              | Canonical display name — may differ from the text the answer used ("Luigis" vs "Luigi's"). For `web`, the source domain ("fifa.com") |
| `uri`  | `string`              | `kebi://{kind}/{key}`, pre-composed so the link handler never parses (a `web` URI carries the URL base64url-encoded, no padding) |
| `icon` | `string \| null`      | Single emoji for the entity's identity (🍕, 🏄), drawn beside the name. **Always the stored row's icon, re-read at answer time (ADR-162)** — a chip never contradicts the screen its tap opens. A venue's comes off its catalog row (ADR-117); an area's off its area row (profiler-picked, ADR-153); a web source's is always 🌐. **Nullable by design** — a row with no icon yet (or an area not yet profiled) ships `null` and the client falls back to its own kind/category mapping |

`recommendation_id` is the turn's consult id — an identifier of the
recommendation itself (tracing, evals), not save ceremony: no endpoint
takes it anymore (ADR-151). `null` on a turn where no place tool ran.

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

`ToolResult` shape: `{ tool: "find_saved" | "suggest_places" | "discover_places" | "research", tool_call_id: string, payload: ConsultResult | ResearchResult }`. The `payload` is a union **discriminated by `tool`**: the three place tools carry a `ConsultResult`, `research` carries a `ResearchResult` (additive — a client that doesn't render research payloads still gets the agent's prose answer).

`ConsultResult` carries `candidates` (each with `place`, `source ∈ {saved, suggested, discovered}`, optional namer `reason`), an `empty_reason` literal when no candidates were produced (e.g. `no_location`, `no_match`), and a `recommendation_id` (a per-recommendation id minted by kebi, surfaced as `data.recommendation_id` for tracing/evals — no endpoint echoes it back, ADR-151).

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

event: message
data: {"content": "tonight is [Luigis](kebi://venue/c0ffee00-…) night", "entities": [{"kind":"venue","key":"c0ffee00-…","name":"Luigis","uri":"kebi://venue/c0ffee00-…","icon":"🍕"}]}

event: done
data: {"tool_calls_used": 1}
```

| Frame            | When emitted                                                                                       | Data shape                                                 |
| ---------------- | -------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| `reasoning_step` | Twice per step over its lifecycle (see below) — the agent, every tool, and the fallback all stream | `ReasoningStep` + stream lifecycle fields (`id`, `status`) |
| `message`        | Once, after the graph completes, if there is text                                                  | `{"content": string, "entities": ChatEntity[]}`            |
| `done`           | Always last — even if no message was produced                                                      | `{"tool_calls_used": integer}`                             |

There is no `tool_result` frame: chat renders text and links, and every
richer view lives on the detail screen a link opens (ADR-136). `content`
and `entities` follow the same rules as the JSON path.

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
  "failure_message": null
}
```

| Field             | Type                                   | Notes                                                                                                                                                                                                                                                                                              |
| ----------------- | -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `status`          | `"pending" \| "completed" \| "failed"` | Envelope-level only. `results` is non-empty **iff** `status == "completed"`                                                                                                                                                                                                                        |
| `results`         | `ExtractPlaceItem[]`                   | `{ place: PlaceCore, confidence: float (0–1) }`. **No per-item `status`** — ADR-071 saves every picker candidate with `approved=False`; the user curates later in the product UI. **No `evidence`** — ADR-093 moved the audit trail to an object-storage ledger so it no longer rides the response |
| `raw_input`       | `string \| null`                       | The original user-supplied string, verbatim                                                                                                                                                                                                                                                        |
| `request_id`      | `string \| null`                       | Correlation id                                                                                                                                                                                                                                                                                     |
| `failure_reason`  | `string \| null`                       | Populated only when `status == "failed"`. One of `unsupported_url`, `empty_input`, `no_candidates`, `all_below_threshold`, `candidate_limit_exceeded`, `pipeline_error`, `save_limit_reached`                                                                                                      |
| `failure_message` | `string \| null`                       | Human-readable diagnostic, only when `status == "failed"`                                                                                                                                                                                                                                          |

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
GET /v1/user/library?q=cang
GET /v1/user/library?category=cafe&visited=false&source=tiktok&limit=20
GET /v1/user/library?sort=name&limit=20
GET /v1/user/library?sort=name&limit=20&cursor=<next_cursor-from-prior-response>
```

| Param          | Type                                | Notes                                                                                                            |
| -------------- | ----------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `q`            | `string` (≤200 chars)               | Free-text search over the **whole** library (ADR-164). See below                                                 |
| `area`         | `string` (geo key)                  | One area by key — pass a `key` value received from kebi (a row's `area.key`, an entity's `key`) verbatim; matched by **prefix**, so a city key includes its neighbourhoods. Malformed → 422 (ADR-165) |
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

**`q` — searching the library (ADR-164).** Matches, case-insensitively and
as a **substring**, against the place's name, its alternative names, its
city, neighbourhood and country, its tag values and its categories. Substring rather
than word-matching because the client searches as the user types: `cang`
must already find Canggu. It is a **predicate, not a relevance query** — it
narrows the rows and never reorders them, so `sort` and `cursor` behave
exactly as they do without it. A blank or whitespace-only `q` means *no
search*, not *no results*. It searches the **entire** library, not the
pages the client has loaded — filtering client-side is what makes a saved
place three pages down report as "no results".

Filters combine with **AND** — `q` narrows an already-filtered view rather
than replacing it, and `?area=` + `q` compose (search *within* an area). Default order is newest-first (`saved_at`
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
  "total": 42,
  "filtered_total": 3
}
```

Each item also carries `area` — the area the place sits in, as something
tappable (ADR-165):

```json
{
  "place": { "…": "PlaceCore" },
  "user_data": { "…": "UserPlace" },
  "claims": [],
  "area": {
    "key": "id/ChIJoQ8Q…/ChIJZZZY…",
    "name": "Canggu",
    "uri": "kebi://area/aWQvQ2hJSm9R…",
    "icon": "🏄",
    "parent": { "key": "id/ChIJoQ8Q…", "name": "Bali", "uri": "kebi://area/aWQvQ2hJSm9…", "icon": null }
  }
}
```

`area` is a **sibling of `place`**, not a field inside `place.location`: the
`uri` is a wire concern and the `icon` comes from the areas table, neither of
which is a property of the stored location. `uri` is pre-composed — the geo
key is slash-hierarchical and passes through a codec, so clients must never
rebuild it from `key`. `area` is `null` when the place's geography is coarser
than a city; that is a data-completeness gap (the client's "elsewhere"
bucket), **not** an unprofiled area — an area with no profile row still gets a
working handle, because its screen renders unprofiled too. The same `area`
field appears on `GET /v1/places/{id}`.

| Field    | Type               | Notes                                                                                                                                                                                                                           |
| -------- | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `places` | `SavedPlaceView[]` | `{ place: PlaceCore, user_data: UserPlace, claims: PlaceNote[] }`. `place` is the complete place shape — live rating/hours don't exist anywhere in the contract (ADR-118). `user_data` is this user's relationship to the place |
| `next_cursor` | `string \| null` | Opaque keyset cursor. Pass it back as `?cursor=` for the next page. **`null` on the last page** |
| `total` | `integer` | The caller's **grand total** of saved places — the whole stash, **independent of the request's filters and pagination** (drives the screen's hero count). Same on every page |
| `filtered_total` | `integer` | How many saves match this request's `q` + filters, **across the whole library** — the `3` in "3 of 84". Server-side by necessity: with keyset paging a client cannot count matches it was never sent (ADR-164). Equals `total` when nothing is narrowing |

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

## GET /v1/places/{place_id}

The place screen behind every venue link (ADR-151). Any place kebi has
surfaced resolves here, **saved or not** — a suggested pick is as openable
as a library row. `place_id` is `places.id`, the `key` on the
`kebi://venue/{id}` link the user tapped (ADR-136).

**Request:** path param only. Plus the `X-Gateway-Token` +
`X-Gateway-User-Id` headers.

```
GET /v1/places/c0ffee00-1111-2222-3333-444455556666
```

**Response (200):** a `LibraryItem` — the **same** `{ place, user_data,
claims, area }` shape as one entry of the library response, so a venue tap and
a library row open the identical screen. The one difference: **`user_data` is
`null` when the caller never saved this place** — that null is the screen's
"offer save" signal (`POST /v1/user/places` with this same id). `claims`
are the place's insider notes (ADR-127): global approved claims plus the
caller's own, strongest first; `from_shared` can only be `true` when the
caller holds a save whose share ref matches — an unsaved place's notes are
all simply global.

```json
{
  "place": {
    /* PlaceCore */
  },
  "user_data": null,
  "claims": [
    {
      "id": "1e7c…",
      "text": "sunset is the slot, daybeds book out on weekends",
      "tags": ["timing"],
      "source": "community",
      "from_shared": false,
      "agree_count": 0,
      "disagree_count": 0
    }
  ],
  "area": {
    "key": "id/ChIJoQ8Q…/ChIJZZZY…",
    "name": "Canggu",
    "uri": "kebi://area/aWQvQ2hJSm9R…",
    "icon": "🏄",
    "parent": { "key": "id/ChIJoQ8Q…", "name": "Bali", "uri": "kebi://area/aWQvQ2hJSm9…", "icon": null }
  }
}
```

`area` is the area this place sits in, as something tappable (ADR-165) — the
same field and same shape a library row carries, minted by the same builder,
so the two can never disagree about an area's name. `null` when the place's
geography is coarser than a city.

> **Lazy enrichment (ADR-152):** a place that entered the catalog through
> the suggestion path opens thin (no experiential tags) the first time —
> that open triggers a background profiling pass, so tags and icon are
> filled within seconds and appear on the next fetch. Clients should not
> treat an empty `place.tags` as permanent.

| Code  | When                                                              |
| ----- | ----------------------------------------------------------------- |
| `200` | The place is in the catalog (saved or not)                        |
| `404` | `place_id` is not in the catalog (`detail: place_not_found`) — venue links kebi minted always resolve, so this means a stale or fabricated id |

---

## GET /v1/areas/{area_id}

The area screen behind every area link (ADR-153). `area_id` is the encoded
geo key carried by the `kebi://area/{id}` link the user tapped — one opaque
URL-safe segment; the raw key still rides the chat entity's `key` field.
Every level of the key hierarchy is openable: country, city/region,
neighbourhood.

Since ADR-169 the key's segments are geo-registry provider ids, not name
slugs — nothing a client can render or construct. Tokens minted before the
change (in old chat messages) still resolve: kebi translates them
server-side, so a stored link never dies. The contract is unchanged either
way: tokens and keys are opaque values you receive from kebi and hand back
verbatim.

**Request:** path param only. Plus the `X-Gateway-Token` +
`X-Gateway-User-Id` headers.

```
GET /v1/areas/aWQvQ2hJSm9R…
```

**Response (200):** the area's **global half** (profile: `name`, `level`,
`icon`, `summary`, `best_for` chips, tappable `breadcrumb`) plus the
caller's **personal half** (`saved_count`, the body `section`), composed
per request and never stored.

`section` is the one block below the profile:

- `kind: "saved"` — the caller has saves under this key. At a wide level
  they group into child-`areas` rows (each with its own `saved_count`,
  `hook`, and tappable `uri`); at neighbourhood level they are venue
  `places` rows (server-composed `subtitle`, the caller's `liked`/`visited`
  for row accents). A save whose place carries no deeper geo than the
  current level appears as a venue row at that level.
- `kind: "worth_knowing"` — no saves here: the profiler's notable child
  areas with one-line hooks. Never venue suggestions — discovery stays in
  chat via the ask bar.
- `null` — nothing to show below the profile (e.g. a save-less
  neighbourhood).

```json
{
  "key": "id/ChIJoQ8Q…/ChIJZZZY…",
  "uri": "kebi://area/aWQvQ2hJSm9R…",
  "name": "Canggu",
  "level": "neighbourhood",
  "icon": "🏄",
  "summary": "the surf-and-laptop end of bali. …",
  "best_for": [{ "icon": "🌅", "text": "sunset drinks" }],
  "breadcrumb": [
    { "key": "id", "name": "Indonesia", "uri": "kebi://area/aWQ" },
    { "key": "id/ChIJoQ8Q…", "name": "Bali", "uri": "kebi://area/aWQvQ2hJSm9…" }
  ],
  "saved_count": 4,
  "profiled": true,
  "section": {
    "kind": "saved",
    "areas": [],
    "places": [
      {
        "id": "c0ffee00-…",
        "name": "Savaya Bali",
        "uri": "kebi://venue/c0ffee00-…",
        "icon": "🍸",
        "subtitle": "beach club · lively",
        "liked": true,
        "visited": true
      }
    ]
  }
}
```

> **Lazy profiling (ADR-153):** an area opens thin the first time
> (`profiled: false` — `summary`/`level`/`icon` null, slug-derived
> name/breadcrumb) and that open triggers a background profiling pass, so
> the dressed screen is there within seconds on the next fetch — the same
> first-open contract as the place screen (ADR-152). The personal fields
> are always live, thin or not.

| Code  | When                                                              |
| ----- | ----------------------------------------------------------------- |
| `200` | The id decodes to a geo key (profiled or not)                     |
| `404` | `area_id` is not a token kebi minted (`detail: area_not_found`)   |

---

## POST /v1/user/places

Save a place kebi surfaced to the caller's library — the plain **"save"**
action on the place screen or a chat venue link (ADR-151). The place already
exists in the catalog (kebi surfaced it), so this only links it to the
caller. `user_id` is taken from `X-Gateway-User-Id`; a caller can only ever
save into **their own** library.

Saving also emits a **positive taste signal** — a _stronger_ one than a
link-share save: its own `saved_recommendation` interaction type, weighted
heavier in the taste evidence and **not** counted toward the discovery-source
distribution (kebi is not a channel the user discovers from). No turn
context is needed for that: the only way a client holds a `places.id` is off
a `kebi://venue/{id}` link kebi produced, so calling this endpoint at all is
what marks the save as kebi-recommended.

**Request:** JSON body + the `X-Gateway-Token` + `X-Gateway-User-Id` headers.

```
POST /v1/user/places
```

```json
{
  "place_core_id": "c0ffee00-1111-2222-3333-444455556666"
}
```

| Field           | Type     | Required | Notes                                                                                |
| --------------- | -------- | -------- | ------------------------------------------------------------------------------------ |
| `place_core_id` | `string` | Yes      | `places.id` of the place — the `key` on the venue link the user tapped (ADR-136) |

> **ADR-151 note:** the retired card ceremony (`recommendation_id`, `reason`)
> is **rejected** as unknown keys (422). The reason-as-claim write (ADR-127)
> retired with it; `user_data.note` is still set only by the user's own edit
> (`PATCH /v1/user/places/{id}`).

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
| `422` | Missing `place_core_id`, or an unknown field (including the retired `recommendation_id`/`reason`)                                                        |

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

## GET /v1/user/library/areas

Which areas the caller's saves fall into, with an exact count each (ADR-165).
The Library screen groups saves by area, and grouping needs what a paged read
cannot give: the complete set of areas, and counts that mean the whole library
rather than the pages loaded so far.

**Request:** identity headers only — no query params.

```
GET /v1/user/library/areas
```

**Response (200):** `LibraryAreasResponse`

```json
{
  "areas": [
    {
      "area": {
        "key": "id/ChIJoQ8Q…/ChIJZZZY…",
        "name": "Canggu",
        "uri": "kebi://area/aWQvQ2hJSm9R…",
        "icon": "🏄",
        "parent": { "key": "id/ChIJoQ8Q…", "name": "Bali", "uri": "kebi://area/aWQvQ2hJSm9…", "icon": null }
      },
      "count": 11
    }
  ]
}
```

| Field   | Type            | Notes                                                                                                                      |
| ------- | --------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `area`  | `AreaHandle`    | Same shape as the `area` on a library row, from the same builder — a heading and its rows can never disagree on a name      |
| `count` | `integer`       | Saves keyed to **exactly** this area, across the whole library. Nested areas are separate entries and are *not* folded in   |

This is **data, not a screen**. Complete and unpaged, with no rollup, no
pinning and no truncation, and **the order carries no meaning** — sort for
whatever the screen needs. It is also always **unfiltered**: it ignores `q`
and every browse filter, because it is the at-rest index and one that narrowed
while someone typed would shift the sections under them. While a search is
active, its counts and the visible rows describe different sets — render the
results flat with `filtered_total` rather than as sections with at-rest counts.

Counts are exact-key while `?area=` matches by **prefix**, and that asymmetry
is deliberate: a client wanting one rolled-up "Bali" heading sums the entries
sharing that `parent` and opens it with `?area={the Bali row's key}`, which returns the
nested rows. Pre-summing here would make the leaf histogram unavailable.

Areas whose geography is coarser than a city are absent entirely — naming that
bucket ("elsewhere") is the client's call.

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

## POST /v1/knowledge/curate

Push expert knowledge into the knowledge layer (ADR-121). The caller writes
prose; kebi's LLM structures it into claims and stores them as
`curated_expert` — **global** knowledge, not scoped to the caller. An
**anchored** request pins the prose to one entity: a venue anchor is what
makes `place`-scoped claims expressible (unanchored prose remains geo-scoped
only), and the anchor's own geography is the fallback when a claim's area
can't be geocoded from the prose.

**Gated:** requires the `X-Gateway-Can-Curate` capability header set to
`true`. Missing/`false` → `403 { "detail": "curation_not_permitted" }` (fail
closed, since these are global writes). `user_id` is taken only from
`X-Gateway-User-Id` and recorded as provenance, never as a claim scope.

(Plus `X-Gateway-Token` + `X-Gateway-User-Id` headers.)

**Request:**

```json
{
  "text": "Cash only at the bar. The whole street around it is great for coffee.",
  "anchor": { "place_id": "5f0c1a2b-..." }
}
```

| Field             | Type     | Notes                                                                                                     |
| ----------------- | -------- | ---------------------------------------------------------------------------------------------------------- |
| `text`            | `string` | Required, non-empty. The expert's prose.                                                                  |
| `anchor`          | `object` | Optional. Exactly one of `place_id` / `area_id` — both or neither inside the object → 422.                |
| `anchor.place_id` | `string` | A catalog place id — the same id `kebi://venue/{id}` links carry and `GET /v1/places/{id}` takes.          |
| `anchor.area_id`  | `string` | An encoded area token — the same token `kebi://area/{id}` links carry and `GET /v1/areas/{id}` takes.      |

`location_hint` was removed when anchors landed: an area anchor is the same
fallback geography, verified. An unknown `place_id` or an undecodable
`area_id` token → `404 { "detail": "anchor_not_found" }` before any LLM
runs. A valid but never-opened area is **not** a 404 (area rows are lazy,
ADR-153).

**Response (200):**

```json
{
  "claims_written": 2,
  "claims": [
    {
      "id": "0b9f4c1e-...",
      "scope": "place",
      "entity_name": "Beach Club X",
      "claim": "Cash only at the bar.",
      "tags": ["cash_only"]
    },
    {
      "id": "7d2e9a03-...",
      "scope": "neighborhood",
      "entity_name": "Jumeirah",
      "claim": "The surrounding street is known for its coffee shops.",
      "tags": ["coffee"]
    }
  ]
}
```

| Field            | Type       | Notes                                                                                                                                                 |
| ---------------- | ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `claims_written` | `integer`  | Count of **new** rows stored. May be less than the prose implied — dedup collapses re-submissions, and unkeyable or accessibility claims are dropped. |
| `claims`         | `object[]` | The stored claims: `{ id, scope, entity_name, claim, tags }`. `id` is the reference `DELETE /v1/knowledge/claims/{id}` takes. Empty when nothing was stored. |

A deduped re-submission does **not** reappear in `claims` — `GET
/v1/knowledge/claims`, not this response, is the source of truth for "what
you've added". Accessibility claims are never stored (an unverified
accessibility claim is real-world harm). Harvested (`shared_content`) and
curated (`curated_expert`) claims about the same entity merge on the same
key and are separable only by their source.

---

## GET /v1/knowledge/claims

One newest-first page of the caller's own curated claims — what backs "what
you've added". **Gated** on `X-Gateway-Can-Curate` like all curation
endpoints. Ownership is the claim's provenance (its `source_ref` names the
author), since curated claims are global rows.

**Query params:** `limit` (default 20, max 100), `cursor` (opaque, from a
prior response's `next_cursor`; malformed → 400).

**Response (200):**

```json
{
  "claims": [
    {
      "id": "0b9f4c1e-...",
      "scope": "place",
      "claim": "Cash only at the bar.",
      "tags": ["cash_only"],
      "created_at": "2026-08-10T12:00:00Z",
      "anchor": { "type": "place", "place_id": "5f0c1a2b-...", "area_id": null, "name": "Beach Club X" }
    },
    {
      "id": "7d2e9a03-...",
      "scope": "neighborhood",
      "claim": "The surrounding street is known for its coffee shops.",
      "tags": ["coffee"],
      "created_at": "2026-08-10T12:00:00Z",
      "anchor": { "type": "area", "place_id": null, "area_id": "YWUvZHViYWkvanVtZWlyYWg", "name": "Jumeirah" }
    }
  ],
  "next_cursor": null
}
```

Every claim carries a renderable, **openable** anchor: a place claim the
catalog id venue links use, a geo claim the encoded token area links use —
group rows by anchor client-side. `created_at` is a raw ISO-8601 instant.
Provenance, confidence, and review internals never leave the service.

---

## DELETE /v1/knowledge/claims/{claim_id}

Retract one of the caller's own curated claims. **Gated** on
`X-Gateway-Can-Curate`. Author-only: the claim's provenance must name the
caller.

**Response:** `204` on success. `404 { "detail": "claim_not_found" }` covers
both a claim that doesn't exist and one that isn't the caller's — the two
are deliberately indistinguishable, so ids can't be probed.

---

## GET /v1/knowledge/entities

Typeahead behind the curation anchor chip — places and areas in one typed
list. **Gated** on `X-Gateway-Can-Curate`. Deterministic (no LLM): catalog
places via unscoped hybrid search, areas from the profiled areas table, and
— only when the known corpus has no area hit — a verified-or-refuse geocode
so a never-opened area can still be anchored. A bare unseen name resolves
as a country or, failing that, as the prominent city of that name via a
structured lookup whose answer must slug-match the typed name ("Tokyo" →
Tokyo, JP); `"Name, Country"` constrains a lesser namesake to its country.
Free-text geocoding is never used, and an unverifiable name returns no
area rather than a guess.

**Query params:** `q` (required, 2–120 chars), `limit` (default 8, max 20).

**Response (200):**

```json
{
  "results": [
    {
      "type": "area",
      "place_id": null,
      "area_id": "aWQvQ2hJSm9R…",
      "name": "Canggu",
      "level": "neighbourhood",
      "icon": null,
      "context": "Bali, ID"
    },
    {
      "type": "place",
      "place_id": "5f0c1a2b-...",
      "area_id": null,
      "name": "Canggu Coffee Lab",
      "level": null,
      "icon": "☕",
      "context": "Canggu, Bali"
    }
  ]
}
```

Areas lead, then places in relevance order. The id field **is** the anchor
payload: `place_id` / `area_id` go into a curate `anchor` verbatim, and each
also opens on its own screen (`GET /v1/places/{id}` / `GET /v1/areas/{id}` —
an unprofiled area opens thin and profiles lazily). `icon` is nullable; the
client keeps its category fallback (ADR-146). No matches is
`{ "results": [] }`, never an error.

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
| POST /v1/chat               | Conversational turn (consult-family agent) | message, optional location, movement_profile, user_profile   | type (`agent`\|`error`), message (with `kebi://` entity links), data (reasoning_steps + entities + recommendation_id), tool_calls_used        |
| POST /v1/chat/stream        | SSE streaming chat                         | Same as POST /v1/chat                                        | reasoning_step + message (content + entities) + done frames                                                                                  |
| GET /v1/home                | Home greeting + suggestion chips           | — (optional `lat`/`lng`/`city`/`local_time`/`weather` query) | HomeResponse (`greeting`, `chips: { text }[]`); fail-open, always `200`                                                                      |
| GET /v1/user/intents        | "What you wanted" recall list              | — (optional `limit`/`cursor` query params)                   | IntentsResponse (`intents: { id, text, created_at }[]`, `next_cursor`)                                                                       |
| POST /v1/extract            | Canonical extraction (save a place)        | raw_input                                                    | ExtractPlaceResponse                                                                                                                         |
| GET /v1/user/library        | Browse + search the user's saved places (Library) | — (optional `q` + `area` + filter + `sort` + `limit`/`cursor` query params) | LibraryResponse (`places: SavedPlaceView[]`, `next_cursor`, `total`, `filtered_total`)                                |
| GET /v1/user/library/areas  | Which areas the user's saves fall into     | — (identity only)                                            | LibraryAreasResponse (`areas: { area: AreaHandle, count }[]`) — complete, unpaged, unfiltered                                                |
| GET /v1/places/{id}         | Open any surfaced place (the place screen) | — (path param only)                                          | LibraryItem (`place`, `user_data` — null when unsaved, `claims`); `404` if uncatalogued                                                      |
| GET /v1/areas/{id}          | Open any linked area (the area screen)     | — (path param only; id = encoded geo key)                    | AreaScreenResponse (profile + breadcrumb + `saved_count` + one body section: saved drill-down or worth-knowing); `404` if the id is no token |
| POST /v1/user/places        | Save a surfaced place (plain save)         | place_core_id                                                | LibraryUserData (created user-state, `201`; `404` if uncatalogued); emits the `saved_recommendation` taste signal                            |
| PATCH /v1/user/places/{id}  | Update a save's user-state (pills/menu)    | partial body: `visited`/`liked`/`approved`/`note`            | LibraryUserData (updated user-state; `200`/`404`)                                                                                            |
| DELETE /v1/user/places/{id} | Remove one saved place from the library    | — (path param only)                                          | 204 No Content (`404` if absent/not owned)                                                                                                   |
| DELETE /v1/user/data        | Account-deletion sweep of AI data          | — (optional `scope` query param)                             | 204 No Content                                                                                                                               |
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
