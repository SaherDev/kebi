# Handoff: entity-anchored curation (ADR-160)

For the product repo. Contract facts and required behavior only — how you
build the screens is yours.

## What changed on the wire

1. **`POST /v1/knowledge/curate` — BREAKING.** `location_hint` is gone; a
   request still sending it gets a 422. Its replacement is an optional
   `anchor` object carrying exactly one of:
   - `place_id` — a catalog place id (the same id `kebi://venue/{id}` links
     carry and `GET /v1/places/{id}` takes), or
   - `area_id` — an encoded area token (the same token `kebi://area/{id}`
     links carry and `GET /v1/areas/{id}` takes). Opaque; never build or
     parse one.

   A venue anchor is what enables claims about the venue itself; unanchored
   prose still works and stays geo-scoped. An unknown `place_id` or an
   undecodable `area_id` → `404 { "detail": "anchor_not_found" }`. Response
   claims now carry an `id`.

2. **New: `GET /v1/knowledge/claims`** — one newest-first page of the
   caller's own curated claims (`limit`, `cursor` keyset paging). Each entry
   is `{ id, scope, claim, tags, created_at, anchor }` where `anchor` is
   `{ type: "place"|"area", place_id, area_id, name }`.

3. **New: `DELETE /v1/knowledge/claims/{claim_id}`** — retract one claim.
   204 on success; 404 for a missing **or** another curator's claim
   (indistinguishable by design).

4. **New: `GET /v1/knowledge/entities?q=&limit=`** — typeahead returning
   `{ results: [{ type, place_id, area_id, name, level, icon, context }] }`,
   areas first, then places. Deterministic and LLM-free; safe to call per
   debounced keystroke (`q` min 2 chars, rate limit 120/min).

   Full shapes: `kebi/docs/api-contract.md` § the four /v1/knowledge
   endpoints; live Bruno requests under `kebi-config/bruno/nestjs-api/`
   (`knowledge-*.bru`).

## Required behavior

- **Gate all four behind the curator role.** Every one requires
  `X-Gateway-Can-Curate: true` and fails closed (403) — the curation doors
  (place-screen menu, area-screen entry, chat venue chip, compose) should
  only render for curator-entitled users.
- **Anchors come from what the user tapped, never typed.** A curation
  entered from a place screen or chat venue chip sends that entity's
  `place_id`; from an area screen, the token out of the area link's URI.
  The free-compose door gets its anchor from the typeahead's results —
  `place_id`/`area_id` there are anchor payloads verbatim.
- **"What you've added" reads the list endpoint, not curate responses.** A
  re-submitted duplicate is deduped server-side and does not reappear in
  the curate response, but it is still in the list under its original id.
  Group rows by anchor client-side; every anchor is openable (place → place
  screen, area → area screen, which may open thin and dress itself,
  ADR-153).
- **Typeahead expectations:** results can be empty (`{"results": []}` is a
  normal answer, not an error). An area the system has never seen resolves
  from a bare name when it's a country or a verifiable prominent city
  ("Tokyo", "Paris"); a lesser namesake needs `"Name, Country"`. A name
  that can't be verified returns no area row — surface a "try adding the
  country" hint in the empty state if you want that discoverable. `icon`
  is nullable; keep the category fallback (ADR-146).
