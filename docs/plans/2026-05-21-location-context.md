# Location Context Model + Start-of-Turn Resolution Flow

**Date:** 2026-05-21
**ADR:** ADR-083
**Status:** complete

## Goal

Give the agent a *working location* — the single, fully-resolved location
(country, city, lat, lng; neighborhood best-effort) a turn operates against —
and resolve it at the start of every turn before the agent reasons.

## What shipped

- `WorkingLocation` / `LocationResolution` models (`core/agent/location.py`).
- `AgentState` gains `user_location`, `working_location` (carry-forward via the
  `merge_working_location` reducer + `LOCATION_INHERIT` sentinel), and
  `location_clarification`. `location` removed.
- `NominatimGeocodingClient` (`core/places/`) — free OpenStreetMap geocoding,
  forward + reverse, no API key.
- `resolve_location` LangGraph node + a keyword gate as the conditional entry
  point: location-free turns skip straight to the agent and pay no resolver
  LLM call.
- `location_resolver` prompt (`config/prompts/`) + model role (`config/app.yaml`).
- `agent.txt` documents shift-detector behavior; `_render_location_context`
  renders the working location or a clarification ask.
- New ADR-083; `docs/api-contract.md` location-field prose updated.

## Resolution rules

- Priority: explicit in message → carried from conversation → user's actual
  location.
- Continuation carries the prior location; naming a new place shifts it.
- Named place → forward geocode; user's actual coords → reverse geocode.
- `explicit_query` / `carried` must resolve all five fields; `user_actual`
  needs country + city + coords (neighborhood best-effort).
- Ambiguous or under-specified → the agent asks the user (no new response type).

## Out of scope

Movement mode; recommendation/recall tool implementation; Redis caching of
geocoding results (fast-follow).

## Verification

`poetry run pytest tests/core/agent tests/core/places tests/core/chat` —
green. `ruff check` / `mypy` clean on touched files. Pre-existing
`test_hybrid_search_repo` REGCONFIG failures are unrelated and out of scope.
