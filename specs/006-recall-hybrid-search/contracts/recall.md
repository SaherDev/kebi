# Contract: POST /v1/recall

**Service**: kebi FastAPI
**Caller**: NestJS backend (after auth verification)
**Branch**: `006-recall-hybrid-search`

## Request

```http
POST /v1/recall
Content-Type: application/json
```

```json
{
  "query": "that cosy ramen place I saved from TikTok",
  "user_id": "usr_abc123"
}
```

| Field | Type | Required | Validation |
|---|---|---|---|
| query | string | Yes | min_length=1; empty → 400 |
| user_id | string | Yes | Trusted from NestJS; not validated here |

## Response — Results Found (HTTP 200)

```json
{
  "results": [
    {
      "place_id": "550e8400-e29b-41d4-a716-446655440000",
      "place_name": "Fuji Ramen",
      "address": "123 Sukhumvit Soi 33, Bangkok",
      "cuisine": "ramen",
      "price_range": "low",
      "source_url": "https://www.tiktok.com/@foodie/video/123",
      "saved_at": "2026-02-12T14:30:00Z",
      "match_reason": "Matched by name, cuisine, and semantic similarity"
    }
  ],
  "total": 1,
  "empty_state": false
}
```

## Response — No Matches (HTTP 200)

User has saved places but none matched the query.

```json
{
  "results": [],
  "total": 0,
  "empty_state": false
}
```

## Response — Cold Start (HTTP 200)

User has zero saved places.

```json
{
  "results": [],
  "total": 0,
  "empty_state": true
}
```

## Error Responses

| Status | Trigger | Body |
|---|---|---|
| 400 | `query` is empty or missing | `{"error_type": "bad_request", "detail": "query is required and cannot be empty."}` |
| 500 | Unexpected internal failure (DB down, etc.) | `{"error_type": "internal_error", "detail": "An unexpected error occurred."}` |

**Note**: Embedding service failure does NOT produce a 5xx. The service falls back to text-only search and returns HTTP 200.

## match_reason Values

| Value | When |
|---|---|
| `"Matched by name, cuisine, and semantic similarity"` | Both vector and text search contributed |
| `"Matched by semantic similarity"` | Vector search only |
| `"Matched by name or cuisine"` | Text search only |
| `"Matched by name or cuisine (semantic unavailable)"` | Text search only; embedding failed at request time |

## Invariants

- `total` always equals `len(results)`.
- `empty_state: true` only when the user has zero saved places, never when results are empty due to no match.
- Results are ordered by RRF score descending.
- Results are always scoped to the requesting `user_id` — no cross-user data ever returned.
- No pagination. Exactly one page of up to `recall.max_results` (default 10) results.
