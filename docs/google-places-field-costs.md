# Google Places API — Field Cost & Usage Reference

Every field kebi requests from Google Places API (New), grouped by Google's
billing layer (SKU tier), plus the fields deliberately dropped by **ADR-118**
(Google = minimal location validator; the LLM knowledge layer owns
experiential tags). Pricing verified 2026-07-09 against
[developers.google.com/maps/billing-and-pricing/pricing](https://developers.google.com/maps/billing-and-pricing/pricing)
and [.../places/web-service/data-fields](https://developers.google.com/maps/documentation/places/web-service/data-fields).

## How billing works

Google bills **per request**, at the tier of the most expensive field in the
`X-Goog-FieldMask` header. One higher-tier field prices the whole request at
that tier — regardless of how many cheaper fields ride along, and regardless
of how many results come back.

| Tier | `:searchText` / `:searchNearby` | Place Details `/{place_id}` | Free calls / month |
| --- | --- | --- | --- |
| Essentials (IDs only) | free | free | unlimited |
| Essentials | $32 / 1k | $5 / 1k | 10,000 |
| Pro | $32 / 1k | $17 / 1k | 5,000 |
| Enterprise | $35 / 1k | $20 / 1k | 1,000 |
| Enterprise + Atmosphere | $40 / 1k | $25 / 1k | 1,000 |

## Current state (ADR-118)

| Call | Mask | Tier billed | Config (`pricing.external.google_places`) |
| --- | --- | --- | --- |
| `:searchText` / `:searchNearby` | `id, displayName, formattedAddress, addressComponents, location, types` | **Pro — $32/1k** (`displayName` is the tier-setter) | `0.032` |
| `/{place_id}` (by-id location refresh) | `id, formattedAddress, addressComponents, location, types` | **Essentials — $5/1k** (no `displayName`; the catalog name is backfilled from the DB row) | `0.005` |

Before ADR-118 both masks requested everything → every call billed
Enterprise + Atmosphere ($40 / $25) while config under-recorded $35 / $20.

---

## Fields kebi requests (all KEEP)

| Google field | Tier | Maps to | Consumed by |
| --- | --- | --- | --- |
| `id` | Essentials (IDs only) | `provider_id` (`google:` prefixed) | Identity everywhere: DB conflict key, cache key, details lookups, dedup |
| `location` | Essentials | `location.lat`/`lng` | Geo filters, distance ranking, map display |
| `formattedAddress` | Essentials | `location.address` | Client display |
| `addressComponents` | Essentials | `city`/`neighborhood`/`country` | Location filters, taste location dimension, embeddings |
| `types` | Essentials | `categories[]` + cuisine tags (`thai_restaurant` → cuisine:thai) + dietary tags (`vegan_restaurant` → dietary:vegan) | Category filters, hard constraints, taste, embeddings, FTS |
| `displayName` | Pro | `place_name` | Required on search (mapper rejects nameless search results); FTS, embeddings, every response. **Not requested on details** — the details path only refreshes already-persisted rows whose name is sticky-authoritative in the catalog |

## Fields deliberately NOT requested (dropped by ADR-118)

### Dead weight — mapped but never consumed (zero readers, verified by trace)

| Google field | Tier it pinned | Was mapped to |
| --- | --- | --- |
| `rating` | Enterprise | `PlaceObject.rating` — Redis cache only, never read |
| `userRatingCount` | Enterprise | `popularity` — never in any ranking |
| `regularOpeningHours` | Enterprise | `hours` — never read; `open_now` had zero callers |
| `nationalPhoneNumber` | Enterprise | `phone` — never read |
| `websiteUri` | Enterprise | `website` — never read |
| `businessStatus` | Pro | dropped even by the cache overlay |
| `timeZone` | Pro | only gated the unread `hours` |

These fields were also removed from `PlaceObject` (now `PlaceCore` +
`cached_at`). Old Redis cache entries still carrying the keys parse fine.

### Experiential data — moved to the LLM knowledge layer

| Google field(s) | Tier they pinned | Tag replaced by |
| --- | --- | --- |
| `priceLevel` | Enterprise | `price` tags inferred by the extraction LLMs from content signals ("so cheap!", tasting-menu framing) or obvious venue identity |
| `dineIn`, `takeout`, `delivery`, `reservable`, `servesBreakfast/Brunch/Lunch/Dinner/Beer/Wine/Cocktails` | Atmosphere | `service` tags from post content + world knowledge of the identified venue |
| `outdoorSeating`, `liveMusic`, `menuForChildren`, `allowsDogs`, `goodForChildren/Groups/WatchingSports` | Atmosphere | `feature` tags, same sources |
| `servesVegetarianFood` | Atmosphere | dietary partially survives via `types` (dedicated `vegan/vegetarian/halal_restaurant` types are Essentials-tier); LLM may add `vegetarian_options` on content evidence |
| `accessibilityOptions` (4 wheelchair sub-fields) | Atmosphere | **Nothing.** Accessibility is categorically forbidden from LLM inference (real-world harm); only previously-attested rows carry wheelchair tags. Deferred: attested re-introduction via targeted lookups |

Constraint semantics changed with this (ADR-118): dietary + accessibility
values stay **hard** filters; all other tag values are preference signals
that steer retrieval/ranking but never exclude a freshly discovered place
whose tags haven't accumulated yet. Saved-places searches still filter
strictly on all values.

## The economics that shaped the decision

- The 7 dead fields cost nothing to drop and bought nothing to keep.
- Atmosphere on a **search** was bulk-priced (+$5/1k covering up to 20
  results) vs $25/1k **per place** via details — so "search cheap, enrich
  later via Google" always loses. The replacement supply is the LLM layer,
  which is ~free (the extraction models already read the content) and
  compounds into proprietary data (vibe tags Google never offered).
- `displayName` on the details call was 100% discarded data: the merge
  policy keeps the catalog name (sticky, existing-wins), so the $12/1k
  Pro premium bought a name we deleted on every call.
- Free tiers (10k Essentials / 5k Pro per month, per SKU) cover early-stage
  volume entirely.

## Fields Google offers that kebi has never requested

| Field | Tier | Why not |
| --- | --- | --- |
| `photos` | Essentials (IDs only) | No image features in the contract |
| `googleMapsUri`, `primaryType`, `utcOffsetMinutes`, `viewport`, `plusCode` | Essentials/Pro | No consumer |
| `internationalPhoneNumber`, `priceRange`, `currentOpeningHours` | Enterprise | Same dead-weight class as the dropped live fields |
| `reviews`, `editorialSummary`, `generativeSummary`, `paymentOptions`, `parkingOptions`, `restroom` | Atmosphere | Never consumed; would re-pin the top tier |
