# Location Kinds — Roadmap

**Status:** direction decided 2026-07-21 · Step 1 done 2026-07-22 (ADR-133) · Step 2 done 2026-08-02 (ADR-134) · Step 3 done 2026-08-02 (ADR-135) · Step 4 is next up
**Scope:** this is a roadmap, not an implementation plan. Each step below is a
self-contained brief — plan and build each one as its own feature, in order.
One plan doc + ADR per step when it starts.

---

## Problems

1. **Everything is a point venue.** The place model has no notion of kind — a
   350 km motorbike loop, a mountain pass, and a café are stored identically:
   one lat/lng, venue categories.
2. **Non-venues leak in through Google's typing.** The validator only blocks
   administrative types; anything Google tags `tourist_attraction` or
   `natural_feature` passes as a "landmark" — how a shared Vietnam video saved
   **Ha Giang Loop** as a venue.
3. **The agent can propose non-venues as venue answers.** Nothing forbids
   routes/passes as candidates — how "trip from Da Nang to Hue" returned
   **Hai Van Pass** as a place.
4. **The legitimate area answers are impossible.** While mislabeled non-venues
   slip through, real ones are blocked: consult cannot recommend a
   neighborhood, city, or region even when that *is* the right answer — only
   research prose exists for area questions.
5. **Trip-shaped queries have no real search.** The corridor concept (ADR-084)
   shapes prose only; search still runs a disc around one point, so "on the
   way" answers come from LLM memory, not data.
6. **Area knowledge is fragmented and ephemeral.** Four subsystems (working
   location, research, home, knowledge) each geocode independently and throw
   the result away; nothing persists, extents (bboxes) are fetched and
   discarded, and there is no shared identity for "Da Nang" across turns or
   tools.
7. **Saves are kind-blind signals.** Saving a route or region updates taste
   like liking a restaurant — a loud interest signal (whole region, experience
   type) is flattened.
8. **The library already contains bad rows** — non-venues stored as point
   venues.

## Goal

**Kebi is a traveler who's local everywhere.** It answers at whatever
granularity the question demands — a café, a neighborhood, a city, a region, a
scenic route — each stored, ranked, and rendered as what it actually is.

- Two stored first-class kinds: **venue, area** — accept-and-type, never
  reject-or-mislabel. **Route is an answer shape, not an entity**: journeys
  are composed by the agent from kebi's own validated venues, knowledge, and
  the user's interests. Externally named routes are never trusted, persisted,
  or shown — they collapse to containing-area interest + experience type.
- **One persistent area authority** shared by every subsystem; the knowledge
  layer remains the rich-data owner (no separate rich-area DB).
- **The user sees places; the engine suggests from interest** — saved
  areas/routes primarily work behind answers as taste signals and geo priors.
  **No trip objects, ever** — connected-places answers are ordered lists,
  never persisted itineraries.
- **One answer contract**: the agent decides per query what kinds the answer
  contains, and a recommended area becomes the conversation's anchor —
  follow-ups zoom in (area → venues inside it), out (venue → its area), and
  across (sibling alternatives).

## Decisions locked (2026-07-21)

- Direction is **accept-and-type**, not reject: areas and routes become
  first-class kinds. Step 1's rejections are a temporary bleed-stopper whose
  detection logic becomes routing in Step 2.
- **No separate rich-area database.** The area entity store holds identity +
  geometry; rich experiential data stays in the knowledge layer (ADR-118
  spirit), linked by entity key.
- **No persisted trips/itineraries.** Kebi stays a decision engine.
- **All granularities** are in scope for area recommendations: neighborhood,
  city, region/country, and routes/experiences.
- **Surfacing**: one answer contract; the agent decides per query which kinds
  the curated list contains (required by the follow-up navigation goal — a
  distinct area-answer type would fight it).
- **Existing mislabeled rows are deleted**, not migrated. Users re-save under
  the correct model.
- Order of work: Step 1 → 2 → 3 → 4 → 5. Steps 3–5 build on 2 but are
  independent of each other.
- **The visible surface is demand-gated (2026-07-21).** Steps 1, 2, 4 and the
  background half of Step 3 (signals, geo priors) proceed unconditionally —
  they're needed under any direction and invisible to the product repo. The
  two user-visible pieces — library kind rendering (Step 3) and areas as
  answers (Step 5) — ship only when research/consult logs show real
  area-granularity demand. Background-only is a staging posture, not a
  terminal state.
- **Never silent drops.** From Step 1 onward, a detected non-venue is
  acknowledged in chat as an interest ("noted for your trip"), never silently
  discarded or saved as a fake venue. A user action always has a visible
  consequence.
- **External routes are untrusted (2026-07-21).** A route name arriving from
  outside (shared content, user text) is never stored, saved, or shown as an
  object — it resolves to its verified containing area plus an
  experience-type interest signal. The only routes the user ever sees are
  journeys the agent composes itself from validated data (Step 4).

---

## Step 1 — Stop the wrong saves  *(done 2026-07-22 — ADR-133, `fix/non-venue-saves`)*

**Problem it closes:** problems 2, 3, 8 — non-venues entering disguised as
venues, from both doors (video extraction and agent suggestions), plus the bad
rows already saved.

**Decided direction:**
- Close the validator gap: results Google types only as non-venue geography
  (e.g. `natural_feature` with no mapped venue category) no longer pass.
- The extraction picker gets a rule: route/area names are not venue candidates.
- The agent's candidate namer gets a rule: never propose a road, pass, loop,
  or region as a place candidate for a venue query.
- Rejection is narrated, not silent: when extraction detects a non-venue, the
  chat acknowledges it as an interest instead of dropping it without a trace.
- Delete existing mislabeled library rows (non-venues stored as venues).

**Constraints:** rejection is temporary by design — write the detection so it
becomes a routing decision ("send to the area path") in Step 2, not throwaway
code. Small enough for a `fix/` branch; no migration beyond the row cleanup.

**Done when:** re-running the two incident scenarios (Vietnam-video share; Da
Nang→Hue trip query) produces no non-venue saves and no non-venue picks, and
the library contains no venue-typed non-venues.

---

## Step 2 — One notion of "an area"  *(done 2026-08-02 — ADR-134, `feature/area-entities`)*

**Problem it closes:** problems 1 and 6 — no kind dimension, and fragmented,
ephemeral area knowledge.

**Shipped direction:**
- A **persistent area entity store** (`area_entities`): entity key in the
  existing claim-key format (`vn`, `vn/hoi-an` — all existing claims attach
  with zero migration; hierarchy via `parent_key`), canonical name + learned
  aliases, country code, centroid, **bbox/extent**, provider feature type.
  Creation path is structured geocode + round-trip verification only (the
  ADR-126 recipe) — never free-text.
- A provider-agnostic geocoding boundary (protocol + adapter). **Deviation:
  Nominatim was removed entirely** — its ~1 req/s public cap can't back
  production; the Google Geocoding API (Essentials tier, $5/1k, 10k
  free/month) backs forward + reverse + the by-place-id geometry refresh.
  All five direct call sites (agent location, corridor, home greeting,
  curator/harvester, research) go through the boundary. Read-through: store
  first, geocode on miss. Reverse sits behind a Redis coordinate-bucket
  cache. ToS compliance: place IDs stored forever; geometry re-geocoded
  through the stored ID when older than 30 days.
- **Deviation: no `kind` column on places** — areas live in their own
  table, `places` stays venue-shaped; revisit at Step 3 if library
  rendering needs it. No `osm:` provider ids (Google place IDs, `google:`
  namespaced, ADR-054).
- **Two-validator routing:** the Step 1 rejection reason is now a subtype
  (`non_venue_area` resolves to itself; `non_venue_route` collapses to its
  containing area). Noted names carry their share's location context
  (ADR-082) into resolution.
- Extraction applies the **subject-vs-container rule** in harvest anchoring;
  no library saves of areas in this step (waits for Step 3 rendering).
- **Harvest from noted-interest-only shares** — closed: noted refs ride the
  harvest snapshot, resolve through the area service, and anchor the
  share's claims; a zero-venue share now harvests. Experience-type tags
  (`experience`: scenic_route, motorbike_route, hiking, …) joined the
  claim-tag vocabulary so route interest survives as tagged area knowledge.
- Curator emits structured area components (country, city) instead of a
  free-text query.

**Constraints held:** knowledge layer stays the rich-data owner — the entity
store holds identity + geometry only. Consult answers stay venue-only.

**Rollout note:** the Geocoding API must be enabled on the GCP project
(one-time, free tier) — without it every geocode refuses and location turns
degrade to clarification asks.

---

## Step 3 — Saves that mean something  *(done 2026-08-02 — ADR-135, `feature/area-signals`)*

**Problem it closes:** problem 7 — kind-blind signals.

**Shipped direction:**
- A share's noted areas now emit their own taste signal class — **region
  interest** — distinct from venue accept/like, resolved to the ADR-134 entity
  (a route collapsing to its containing area). Route/region shares also
  contribute **experience-type** signals (scenic route, motorbike route,
  hiking, …) with no saved object. Both are positive-only, fire automatically
  off the share through the existing background harvest pass, and land in
  their own top-level `signal_counts` buckets — never folded into the
  venue-derived location context, which is what lets taste tell "interested in
  a region" from "liked a restaurant".
- **Geo prior is soft:** because the taste summary already feeds the agent
  prompt and the candidate namer, region/experience interest biases later
  open-ended suggestions with no new retrieval code.
- **Deviations (in ADR-135):** trigger is **automatic from shares — no
  explicit save action, no library rendering** (the user-visible half stays
  demand-gated per Decisions locked); **soft prior only — hard extent-scoping
  deferred to Step 4/5** (it rewrites the same geofence Step 4 owns); region
  interest is harvest-LLM-independent while experience specificity rides
  best-effort harvest output.

**Rollout note:** one enum-values migration (`alembic upgrade head`) adds the
two interaction types; nothing else rolls over (no Redis invalidation, no
contract change).

**Done when:** a shared route influences later suggestions in its region, and
the taste model distinguishes "showed interest in a region" from "liked a
restaurant." *(met)*

---

## Step 4 — Trip-shaped queries answer from data

**Problem it closes:** problem 5 — corridor is prose-only.

**Decided direction:**
- Corridor search becomes real geometry: both endpoints are stored entities;
  sample waypoints between centroids (count scaled by corridor length); union
  of the existing disc searches at each waypoint; dedup; order results by
  progress along the route.
- The candidate namer receives corridor context so proposals are along the
  way.
- The answer stays within the existing one-list contract (ADR-091), ordered by
  route progress and narrated as a journey.

**Constraints:** straight-line waypoint sampling is v1 — road-shape routing is
explicitly out of scope (OSM routing exists if it ever matters). No new
geo infrastructure; reuse the existing radius-search path.

**Done when:** "trip from Da Nang to Hue" returns an ordered set of real,
validated stops along the route instead of one famous landmark.

---

## Step 5 — Areas as recommendations + kind navigation

**Problem it closes:** problem 4 — legitimate area answers are impossible.

**Decided direction:**
- Consult can put an **area forward as the answer**, at any granularity:
  neighborhood ("where should I stay?"), city ("Hoi An or Hue?"),
  region/country ("where in November?"). Route-shaped asks ("scenic drive
  near Da Nang") get an agent-composed journey (the Step 4 shape) — never a
  stored route object. Kebi answers like a traveler who's local everywhere.
- One answer contract: the agent decides per query which kinds the curated
  list contains; each item carries its kind (plus extent for non-venues) for
  rendering.
- Rendering principles: an area renders as a shaded extent on the map — never
  a pin — with a one-line why and a zoom-in affordance ("show places here");
  a composed journey renders as ordered venue stops along the way (there is
  no route card). Every answer bottoms out in venues; area cards are
  doorways, not dead ends. No kind jargon or entity internals in the UI.
- The recommended entity becomes the **conversation anchor** (generalizing
  ADR-131's conversation-scoped research area into one shared notion across
  tools). Follow-up navigation:
  - **zoom in** — "good cafés there?" → venue consult scoped to the anchor's
    extent (real geometry, not string matching);
  - **zoom out** — a venue's containing area, via the entity hierarchy;
  - **zoom across** — sibling areas under the same parent, ranked by taste.
- Area ranking draws on the knowledge layer's claims + the taste model — the
  rich data accreted since Step 2.

**Done when:** "which neighborhood should I stay in?" returns a ranked area
answer, and "what's good to eat there?" as the next turn searches inside it.

---

## Out of scope (whole roadmap)

- Persisted trips/itineraries — decided no; Kebi is a decision engine.
- A second rich-area database — the knowledge layer owns rich data.
- Road-shape corridor routing — straight-line sampling is v1.
- Mixed-kind consult answers before Step 5.
- Trusting or persisting externally named routes — route names collapse to
  containing-area interest; journeys are agent-composed only.
