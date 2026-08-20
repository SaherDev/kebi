# Handoff: geo keys became opaque identity paths (ADR-169)

For the product repo. Contract facts and required behavior only — no
endpoint was added, removed, or reshaped; every field name, frame, and URI
scheme is exactly what it was. What changed is the *values* inside geo keys
and area tokens, and one guarantee that got stronger.

## What changed on the wire

1. **Area `key` values are no longer readable.** An area's `key` (on chat
   entities, `area.key` on library rows, `parent.key`, breadcrumb items,
   the `?area=` filter) used to look like `id/bali/canggu`. It is now a
   provider-id path like `id/ChIJoQ8Q…/ChIJZZZY…`. Same shape (`/`-nested
   string), same prefix semantics (a city key contains its neighbourhood
   keys), but the segments are meaningless to humans.
2. **Area tokens changed contents, not shape.** `kebi://area/{token}` and
   `GET /v1/areas/{id}` still carry one opaque URL-safe segment; the bytes
   inside now encode the id path. Tokens you stored or cached from before
   the change **still resolve** — kebi translates old tokens server-side,
   forever, so links in old chat messages keep working with no client
   action.
3. **Names come with every key.** Everywhere a key appears, the display
   name rides beside it (`name` on entities, handles, breadcrumbs) — and
   names are now better: colloquial where locals use one ("Canggu" for the
   stretch officially named Tibubeneng), and per-island where one admin
   name covered several (Gili Air is Gili Air, not "Gili Trawangan").

## Required behavior

- **Treat keys and tokens as opaque, round-trip verbatim.** If anything on
  your side parses a geo key's segments, derives a display string from a
  key, slugifies a name into a key, or reconstructs a `kebi://area/...`
  URI — that must stop; it was already against the contract and now yields
  garbage. `name` fields are the only displayable text; `key`/`uri`/token
  values are only ever passed back to kebi as received.
- **Don't equality-match stored keys across the migration.** If you cached
  or persisted area keys client-side (filters, favorites, deep links), a
  pre-change value no longer equals the post-change value for the same
  area. Raw *tokens* keep resolving via `GET /v1/areas/{id}`; raw *keys*
  from before the change should be dropped or refreshed from a new
  response, not compared.
- **Nothing else.** Pagination, filters, entitlements, SSE frames, place
  ids, venue and web links are all untouched.

## Sequencing

The data migration on kebi's side rewrites stored keys once, right after
the deploy. Between deploy and migration (minutes), an area may briefly
appear as two groups in the library index; it self-heals when the
migration completes. No product-repo deploy is required at any point.
