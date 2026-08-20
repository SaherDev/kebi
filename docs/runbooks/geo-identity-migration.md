# Geo identity migration — one-off prod run

Moves production from slug geo keys to registry id keys. Run ONCE, right
after the deploy that ships the `geo_areas` schema and the registry code.
Until it runs, rows written before the deploy keep their slug keys: old
area links still resolve (legacy decode), but an area can render as two
groups (old-keyed rows vs new-keyed rows) — so run it immediately after
the deploy, not "later".

## Preconditions

- The deploy is live and healthy (`GET /v1/health`).
- `alembic upgrade head` ran as part of the deploy (the `geo_areas` /
  `geo_area_aliases` tables exist). Verify:
  `SELECT count(*) FROM geo_areas;` succeeds.
- Before this whole feature: confirm whether the Gili fold rederive ever
  ran after commit 96e9aff — if it did not, nothing extra is needed; this
  migration supersedes it either way.
- `GOOGLE_API_KEY` and `OPENAI_API_KEY` are set in the environment the
  script runs in (it mints through the Geocoding API and the
  `area_registry` LLM role).
- No long-running open transactions holding locks (the ADR-166 failure):
  `SELECT pid, state, now() - xact_start AS age FROM pg_stat_activity
   WHERE state <> 'idle' ORDER BY age DESC LIMIT 5;`

## Procedure

1. **Dry run first**, against prod, from a machine with the prod
   `DATABASE_URL` exported:

       poetry run python -m scripts.migrate_geo_identity --dry-run

   Read the report: legacy key count, mapped vs unresolvable (the
   unresolvable list prints — a handful of junk keys is normal; a large
   list is a stop-and-look). NOTE: even the dry run WRITES registry rows
   (mints are idempotent and correct either way); it does not touch
   places/claims/areas.

2. **Apply**:

       poetry run python -m scripts.migrate_geo_identity

3. **Verify** (all should be zero / sane):

       -- no multi-segment slug keys left on places
       SELECT count(*) FROM places
       WHERE geo_key ~ '^[a-z]{2}(/[a-z0-9][a-z0-9-]*)+$';

       -- no slug-keyed geo claims left
       SELECT count(*) FROM knowledge_claims
       WHERE entity_key NOT LIKE 'place:%'
         AND entity_key ~ '^[a-z]{2}(/[a-z0-9][a-z0-9-]*)+$';

       -- every place key resolves to a registry row
       SELECT count(*) FROM places p
       WHERE p.geo_key IS NOT NULL
         AND NOT EXISTS (SELECT 1 FROM geo_areas g WHERE g.geo_key = p.geo_key);

       -- legacy decode works: pick any old key from the mapping output
       SELECT geo_key FROM geo_areas WHERE legacy_key = 'id/bali/canggu';

4. **Smoke** the product surfaces: open the library grouped by area, open
   a Bali area screen, tap an area link in an OLD chat message (must land
   on a screen, not 404), and run one chat turn that names an area.

## Rollback

The script only rewrites derived values (`places.geo_key`,
`knowledge_claims.entity_key`, `areas` keys) — source data (locations,
claim text) is untouched. If something looks wrong, the fix is a
corrected re-run (idempotent), not a restore. `alembic downgrade` of the
schema migration drops the registry tables; only do that together with
reverting the code deploy.

## Cost

One Geocoding call per unique area in the data (plus split/group
verification) + one gpt-4o-mini call per minted row. At current data
volume this is tens of calls — inside Google's 10k/month free tier.
