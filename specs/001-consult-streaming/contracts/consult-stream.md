# API Contract: POST /v1/consult (streaming mode)

**Version**: 1.1 — adds optional `stream` parameter
**Caller**: NestJS product service
**Server**: kebi FastAPI

---

## Request

```json
POST /v1/consult
Content-Type: application/json

{
  "user_id": "string",
  "query": "good ramen near Sukhumvit for a date night",
  "location": {
    "lat": 13.7563,
    "lng": 100.5018
  },
  "stream": true
}
```

**Schema changes from v1.0:**
- Added `stream` (boolean, optional, default `false`)
- All other fields unchanged

---

## Response: Streaming Mode (`stream: true`)

```
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
X-Accel-Buffering: no

data: {"token": "Sure"}

data: {"token": ","}

data: {"token": " here"}

data: {"token": " are"}

data: {"token": " some"}

data: {"token": " great"}

data: {"token": " ramen"}

data: {"token": " spots"}

data: {"token": "..."}

data: {"done": true}

```

**Event sequence:**
- N token events: `{"token": "..."}` — one per AI-generated token, emitted as produced
- 1 done event: `{"done": true}` — signals stream completion

**Termination**: Stream closes immediately after the `done` event. Connection is not kept alive.

---

## Response: Synchronous Mode (`stream: false` or absent)

```json
HTTP/1.1 200 OK
Content-Type: application/json

{
  "primary": {
    "place_name": "Stub Place",
    "address": "123 Test St",
    "reasoning": "Stub response",
    "source": "saved"
  },
  "alternatives": [],
  "reasoning_steps": [
    {"step": "intent_parsing", "summary": "Parsing intent..."},
    {"step": "ranking", "summary": "Ranking candidates..."}
  ]
}
```

---

## Error Handling

Same error table as v1.0:

| Status | Meaning | When |
|--------|---------|------|
| 400 | Bad request | Malformed JSON, missing required field |
| 422 | Unprocessable | Cannot parse query or no results |
| 500 | Internal error | Unexpected server failure |

Streaming errors: If a failure occurs mid-stream, the stream closes without a final `done` event. Callers should treat a stream that ends without `done: true` as an error condition.

---

## Backward Compatibility

Clients omitting `stream` get the existing synchronous JSON response. **No breaking change.**

---

## Bruno Test File

`kebi-config/bruno/ai-service/consult-stream.bru`
