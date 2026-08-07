# Handoff: the area screen (ADR-153)

For the product repo. Contract facts and required behavior only — how you
build the screen is yours.

## What changed on the wire

1. **Area links are now encoded.** A chat entity of `kind: "area"` still
   carries the raw geo key in `key` (e.g. `id/bali/canggu`), but its `uri`
   is now `kebi://area/{opaque-token}` (e.g.
   `kebi://area/aWQvYmFsaS9jYW5nZ3U`). Treat the token as opaque; never
   parse it. If you were splitting the old slash-path URI, stop — `key` has
   the raw value.
2. **New endpoint: `GET /v1/areas/{id}`** — the screen behind every area
   link. `{id}` is exactly the token from the link's `uri` path. Standard
   gateway headers. Full request/response shape:
   `kebi/docs/api-contract.md` § GET /v1/areas/{area_id}; live Bruno
   request: `kebi-config/bruno/ai-service/area-detail.bru`.

## Required behavior

- **Every area link opens this screen** — country, region, or
  neighbourhood; breadcrumb items and child-area rows in the response are
  themselves `{key, name, uri}` entities and must be tappable, opening the
  same screen for their own key.
- **First open may be thin** (`profiled: false`): no summary/level/icon,
  slug-derived name. That very open triggers generation server-side; the
  dressed profile exists within a few seconds. Render a skeleton or the
  thin header — do not treat thin as an error, and do not poll aggressively
  (one refetch on re-entry is enough).
- **The body is one section, server-chosen** (`section.kind`):
  - `saved` — the user's own footprint: child-area rows (`areas`, each
    with `saved_count` and a one-line `hook`) at wide levels, venue rows
    (`places`) at neighbourhood level. Both lists can be non-empty at once
    (a save with no deeper geo shows as a venue row at the wide level).
  - `worth_knowing` — same child-area row shape, but kebi's notable picks;
    the user has no saves here. `saved_count` is 0 on these rows.
  - `null` — render profile + ask bar only.
- **Venue rows** carry `uri` (`kebi://venue/{id}`) → the existing place
  screen; `subtitle` comes pre-composed; `liked`/`visited` are for row
  accents (dots/pills), not for you to recompute.
- **`saved_count` is personal** — the caller's saves under this area, not
  a global stat. The chip reads "N saved".
- **Ask bar:** the screen should offer a chat input pre-anchored to the
  area (e.g. placeholder "ask about canggu…"); sending goes to the normal
  `POST /v1/chat` with the user's text — no special parameter exists.
- **404** (`area_not_found`) only happens for ids kebi never minted —
  treat as a dead link, not a retry case.

## Acceptance checks

- Tapping "Canggu" in a chat answer opens a screen titled Canggu with
  breadcrumb `indonesia › bali`, summary + best-for chips (after first
  dressing), and the user's saved places there.
- Tapping "Bali" shows child areas with counts (not a flat venue list);
  tapping a child row drills in.
- A brand-new user tapping "Bali" sees the profile plus "worth knowing"
  children.
- An area never surfaced by kebi (hand-typed URL) 404s gracefully.
