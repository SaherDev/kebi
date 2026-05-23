# Movement & search-scope

**Date:** 2026-05-22
**ADR:** ADR-084
**Status:** complete

## Goal

Give every agent turn a **search scope** — an effective movement mode, a scope
tier, a shape, and from those a concrete search radius — folded into the
working-location resolution (ADR-083). A point alone cannot ground a distance;
"near me" is half a kilometre on foot and several by car, and reaches further
in a sparse town than a dense city.

## What shipped

- `MovementProfile` request field on `POST /v1/chat` — `{ available_modes,
  reach }`, owned by the product repo's `user_settings`, sent each turn like
  `location`. Modes: `walking | cycling | motorbike | driving | transit |
  rideshare`. `available_modes` is the user's **capability** (modes they can
  use), not per-city availability — see ADR-086. kebi consumes, never stores
  it.
- `WorkingLocation` gains `effective_mode`, `scope_tier`, `scope_shape`,
  `search_radius_m`, `corridor`, `density`, `bbox` — carried across turns.
- `LocationResolution` gains `scope_tier`, `scope_shape`, `effective_mode`,
  `corridor_destination` — the resolver classifies; it never emits the radius.
- `resolve_radius()` — deterministic: `radius_tiers[shift(tier, reach)] ×
  mode_multiplier[mode] × density_factor[density]`, all config-driven.
- `density_class()` — maps the geocoder's place type (city / town / village)
  to dense / medium / sparse. No static table.
- `GeocodeResult` — the geocoding client now returns coordinates plus the
  place type (density proxy) and bounding box, reused from the same response.
- `resolve_location` node extended: after the point resolves, it folds in the
  effective mode, scope tier/shape, density, and radius; a corridor
  destination is eagerly geocoded.
- `movement:` config block (`radius_tiers`, `mode_multiplier`,
  `density_factor`, `fallback`); `MovementConfig` schema.
- Agent prompt gains a `{movement_context}` slot; resolver prompt gains a
  mobility-profile slot and scope/mode classification rules with examples.

## Resolution rules

- Effective mode: an explicit per-turn mode word ("if I drive") wins, even
  outside `available_modes`; otherwise the resolver picks from
  `available_modes` using the working location's city and density (ADR-086 —
  Manhattan → transit, Koh Samui → motorbike/driving). If the resolver
  leaves it empty, the system falls back to `available_modes[0]`.
- Scope tier: walkable / neighborhood / city / metro, classified from request
  context; `reach` shifts it ±1.
- A different city/country is a location *shift* (ADR-083), not a wider tier —
  the point re-anchors, scope resolves fresh there.
- Corridor ("on my way…"): the destination is eagerly geocoded; an
  ungeocodable / implicit anchor ("home") asks the user, never a silent
  fallback to an area search.
- No `movement_profile` on the request → a neutral config fallback keeps the
  radius math working; the agent is told to ask when distance is load-bearing.

## Out of scope / follow-up

- Place search consuming `search_radius_m` — ADR-075 removed recall; scope is
  recorded for the future consumer and shapes only the agent's prose today.
- Corridor-aware search geometry (route, not disc).
- Finer-grained density than the three-way place-type class.

## Verification

`poetry run pytest` (full suite green except the pre-existing `places_v2`
`hybrid_search_repo` REGCONFIG failures), `ruff check`, `ruff format --check`,
`mypy src/` (no new errors — the 5 pre-existing ones are unrelated).
