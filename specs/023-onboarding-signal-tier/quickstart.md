# Quickstart — Feature 023: Onboarding Signal Tier

Dev walkthrough to exercise every tier end-to-end after implementation lands. Assumes local `docker compose up -d` (PostgreSQL + Redis) and a valid `.env` symlink to `kebi-config/secrets/ai.env.local`.

---

## 1. Bring up the service

```bash
docker compose up -d                                   # PostgreSQL + Redis
poetry install                                         # sync deps
poetry run alembic upgrade head                        # applies chip_confirm + metadata migration
poetry run uvicorn kebi.api.main:app --reload
```

Verify migration landed:

```bash
poetry run alembic current                             # should list the new revision
```

---

## 2. Cold tier — new user reads /v1/user/context

```bash
# Nothing in the DB for this user.
curl -s http://localhost:8000/v1/user/context?user_id=cold_user | jq
# → { "user_id": "cold_user", "saved_places_count": 0, "signal_tier": "cold", "chips": [] }
```

The product repo sees `signal_tier="cold"` and renders its onboarding UI directly. It does not call `/v1/consult`. No LLM call fires in this repo for a cold user. Confirm in Langfuse that no `consult.*` spans exist for this user between app startup and the first save.

---

## 3. Warming tier — seed a few saves

```bash
# Simulate 3 saves via the extraction endpoint (or seed directly).
# After saves settle and the debounced regen fires:
curl -s http://localhost:8000/v1/user/context?user_id=warm_user | jq
# → signal_tier == "warming", chips may contain a few pending items
```

Now send a consult:

```bash
curl -s -X POST http://localhost:8000/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"user_id": "warm_user", "message": "Thai food nearby"}' | jq
# → ChatResponse.type == "consult"
# → data.results contains ≤ 2 saved items and up to 8 discovered items (warming_blend 0.2/0.8 of total_cap=10)
```

Verify candidate mix in the response `results[].source` distribution.

---

## 4. Chip selection tier — user crosses round_1 threshold

After 5+ saves have accumulated:

```bash
curl -s http://localhost:8000/v1/user/context?user_id=chip_user | jq
# → signal_tier == "chip_selection"
# → chips: array of pending items with status="pending", selection_round=null
```

The product repo sees `signal_tier="chip_selection"` and renders the chip-selection UI directly from the `chips` array in the response. It does NOT call `/v1/consult` while the user is in this tier.

---

## 5. Submit chip_confirm

```bash
curl -s -X POST http://localhost:8000/v1/signal \
  -H 'Content-Type: application/json' \
  -d '{
    "signal_type": "chip_confirm",
    "user_id": "chip_user",
    "metadata": {
      "round": "round_1",
      "chips": [
        {"label":"Finds places on TikTok","signal_count":5,"source_field":"source","source_value":"tiktok","status":"confirmed","selection_round":"round_1"},
        {"label":"Ramen lover","signal_count":3,"source_field":"attributes.cuisine","source_value":"ramen","status":"confirmed","selection_round":"round_1"},
        {"label":"Casual spots","signal_count":2,"source_field":"attributes.vibe","source_value":"casual","status":"rejected","selection_round":"round_1"}
      ]
    }
  }' | jq
# → { "status": "accepted" }   (202)
```

**Server side**:
- One `interactions` row with `type='chip_confirm'`, `metadata` populated.
- `taste_model.chips`: matched chips updated; unmatched chips preserved.
- Background handler runs the four-section taste regen (visible in Langfuse as `taste.chip_confirmed_regen` span).

Re-read context:

```bash
curl -s http://localhost:8000/v1/user/context?user_id=chip_user | jq
# → signal_tier == "active" (no more pending chips)
# → chips reflect new statuses; confirmed items have selection_round="round_1"
```

---

## 6. Active tier — personalized consult

```bash
curl -s -X POST http://localhost:8000/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"user_id": "chip_user", "message": "where to eat?"}' | jq
# → ChatResponse.type == "consult"
# → data.results ranked with confirmed chips as positive signals
# → rejected-chip matches excluded from candidate pool
```

---

## 7. Verify idempotent rewrite

Repeat the same chip_confirm twice within a few seconds:

```bash
# Expected: two interaction rows, two dispatched events, final taste_profile_summary identical
psql $DATABASE_URL -c "SELECT COUNT(*) FROM interactions WHERE user_id='chip_user' AND type='chip_confirm';"
# → 2
psql $DATABASE_URL -c "SELECT taste_profile_summary FROM taste_model WHERE user_id='chip_user';"
# → same content after each run
```

---

## 8. Verify confirmed chips never mutate

After any number of further SAVE/ACCEPTED/REJECTED interactions:

```bash
# Watch a confirmed chip — its status and selection_round never change.
psql $DATABASE_URL -c "SELECT jsonb_pretty(chips) FROM taste_model WHERE user_id='chip_user';"
# Chips with status='confirmed' keep selection_round='round_1' forever.
# Rejected chips may flip back to pending if signal_count grew.
```

---

## 9. Bruno

Added: `kebi-config/bruno/signal-chip-confirm.bru`. Open in Bruno, point at `localhost:8000`, run with a seeded chip_selection user to verify the happy path without leaving the IDE.

---

## Verify commands (from spec)

- `poetry run alembic upgrade head` — clean apply.
- `poetry run pytest tests/core/taste/test_tier.py` — table-driven tier derivation.
- `poetry run pytest tests/core/taste/test_chip_merge.py` — merge invariants.
- `poetry run pytest tests/api/routes/test_signal.py -k chip_confirm` — endpoint happy path + 422 paths.
- `poetry run pytest tests/api/routes/test_user_context.py` — response shape and signal_tier across tiers.
- `poetry run pytest tests/core/consult/test_service.py -k warming` — warming-tier discovery/saved candidate blend.
- `poetry run pytest tests/core/consult/test_service.py -k active` — active-tier rejected-chip exclusion + confirmed reasoning step.
- `poetry run ruff check src/ tests/` / `poetry run mypy src/` — must pass.
