# API Contract Change: POST /v1/extract-place (Run 3)

**Status**: BREAKING CHANGE — NestJS must update before production deploy
**Affects**: `docs/api-contract.md` (this repo) + `kebi-config/bruno/extract-place.bru`
**Coordination required**: Update NestJS client before deploying Phase 12

---

## New Response Shape

### Saved (inline path — one or more places resolved)

```json
{
  "provisional": false,
  "places": [
    {
      "place_id": "550e8400-e29b-41d4-a716-446655440000",
      "place_name": "Fuji Ramen",
      "address": "123 Sukhumvit Soi 33, Bangkok",
      "city": "Bangkok",
      "cuisine": "ramen",
      "confidence": 0.87,
      "resolved_by": "llm_ner",
      "external_provider": "google",
      "external_id": "ChIJN1t_tDeuEmsRUsoyG83frY4"
    }
  ],
  "pending_levels": [],
  "extraction_status": "saved",
  "source_url": "https://www.tiktok.com/@foodie/video/123"
}
```

### Duplicate (all candidates already saved)

```json
{
  "provisional": false,
  "places": [],
  "pending_levels": [],
  "extraction_status": "duplicate",
  "source_url": "https://www.tiktok.com/@foodie/video/123"
}
```

### Provisional (no inline candidates — background extraction running)

```json
{
  "provisional": true,
  "places": [],
  "pending_levels": ["subtitle_check", "whisper_audio", "vision_frames"],
  "extraction_status": "processing",
  "source_url": "https://www.tiktok.com/@foodie/video/123"
}
```

---

## Fields Removed

| Old field | Why removed |
|-----------|-------------|
| `place_id: str \| None` | Replaced by `places[].place_id` |
| `place: PlaceExtraction` | Replaced by per-place fields in `places[]` |
| `confidence: float` | Replaced by `places[].confidence` |
| `requires_confirmation: bool` | Replaced by `provisional: bool` + `extraction_status` |

---

## Fields Added

| Field | Type | Description |
|-------|------|-------------|
| `provisional` | `bool` | True when background extraction is still running |
| `places` | `list[SavedPlace]` | All places saved in this request (empty on provisional/duplicate) |
| `pending_levels` | `list[str]` | Enricher levels still processing (populated only when provisional=true) |
| `extraction_status` | `str` | `"saved"` \| `"processing"` \| `"duplicate"` |

---

## extraction_status Values

| Value | Meaning |
|-------|---------|
| `"saved"` | One or more places written to DB; `places` is non-empty |
| `"processing"` | No inline result; background enrichers are running; `provisional=true` |
| `"duplicate"` | All candidates already in DB; no new writes; `places` is empty |

---

## Error Responses (unchanged)

| Status | Type | Trigger |
|--------|------|---------|
| 400 | `bad_request` | `raw_input` is empty |
| 500 | `extraction_error` | oEmbed timeout, Places API failure, DB write failure |

Note: `422 unsupported_input` is removed — the cascade architecture handles all input types without raising; unknown inputs fall through to ProvisionalResponse.

---

## Bruno File Update Required

Update `kebi-config/bruno/extract-place.bru` to:
1. Remove assertions on `place_id`, `place`, `confidence`, `requires_confirmation`
2. Add assertions on `provisional`, `places`, `extraction_status`, `pending_levels`
3. Add a second request scenario: TikTok URL → assert `places` length > 0
