# Location Kinds — Roadmap

**Status:** direction decided 2026-07-21 · Step 1 done 2026-07-22 (ADR-133) · Step 2 done 2026-08-02 (ADR-134) · Step 3 done 2026-08-02 (ADR-135) · Steps 4–6 re-scoped 2026-08-02 · Step 4 is next up
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
9. **An answer is final, not workable.** Every answer is delivered as a
   finished output. A user who says "take that one out", "change this stop",
   "find me an alternative" has no path — the only move is to re-ask from
   scratch, losing everything that was right about the answer. This bites
   hardest on the multi-stop journeys Step 4 makes possible.

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
  contains, and each item carries its kind for rendering.
- **An answer is a working set, not a verdict.** Whatever kebi puts forward —
  one venue, an area, a multi-stop journey — the user can revise in
  conversation: drop it, swap it, ask for an alternative. The revision lives
  for the conversation and is never persisted.

## Decisions locked (2026-07-21, revised 2026-08-02)

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
  the curated list contains — a distinct area-answer type would fragment it.
- **Existing mislabeled rows are deleted**, not migrated. Users re-save under
  the correct model.
- Order of work: Step 1 → 2 → 3 → 4 → 5 → 6, in sequence. Step 5 depends on 4
  (revising a one-item answer is thin; the journeys worth revising come from
  corridor geometry). Step 6 depends on 5 for the item shape.
- **Areas as answers are no longer demand-gated (revised 2026-08-02).** The
  original gate existed because rendering an area needs a card the app didn't
  have; the app now renders both venues and areas, so the condition is gone
  and Step 6 is scheduled work like any other step. **Library kind rendering
  stays gated** — nothing in this roadmap depends on it, and no demand for it
  has appeared.
- **Never silent drops.** From Step 1 onward, a detected non-venue is
  acknowledged in chat as an interest ("noted for your trip"), never silently
  discarded or saved as a fake venue. A user action always has a visible
  consequence.
- **External routes are untrusted (2026-07-21).** A route name arriving from
  outside (shared content, user text) is never stored, saved, or shown as an
  object — it resolves to its verified containing area plus an
  experience-type interest signal. The only routes the user ever sees are
  journeys the agent composes itself from validated data (Step 4).
- **A journey has no home but the conversation (2026-08-02).** There is no
  route table and there will not be one: a journey is a tool result plus the
  agent's ordering, held in session state for one conversation and then gone.
  This is what "no trip objects, ever" means in practice. The user can still
  save individual venues out of a journey — that path already exists and is
  venue-shaped, so it needs no change.
- **Kind navigation is deferred (2026-08-02).** The conversation anchor and
  zoom in / out / across were cut from the area-answer work. Areas can *win*
  an answer without being *navigable*. Deferring them also resolves the Step 3
  geofence question: zoom-in was hard extent-scoping's only consumer, so
  corridor geometry (Step 4) is now the roadmap's sole geometry consumer and
  region interest stays a soft prior.

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
geo infrastructure; reuse the existing radius-search path. The journey is
composed at answer time and never persisted — corridor search returns
validated venues, the agent supplies the ordering and the narration.

**Note:** with kind navigation deferred, this is the roadmap's only consumer
of real geometry — the hard extent-scoping deferred out of Step 3 lands here
or nowhere. A corridor endpoint that resolves to a venue rather than an area
has no `area_entities` row; decide at plan time whether such endpoints
degrade to their containing area or are handled directly.

**Done when:** "trip from Da Nang to Hue" returns an ordered set of real,
validated stops along the route instead of one famous landmark.

---

## Step 5 — Answers you can work on

**Problem it closes:** problem 9 — answers are final, not workable.

**Decided direction:**
- Whatever kebi puts forward becomes a **working set the user revises in
  conversation**. Four operations: **remove** ("take that one out"),
  **alternative** ("find me something else for this"), **add** ("put a coffee
  stop between 2 and 3"), **reorder** ("museum before lunch").
- **Every answer is revisable, ops adapt to shape.** Remove and alternative
  apply to any answer — a single venue, an area, a journey. Add and reorder
  only mean anything when the answer is a multi-item list, and only surface
  there.
- **The working set lives in session state (Redis), never a row.** With no
  route table by design, there is nowhere else for it to be — and this repo
  owns Redis exclusively. A new consult in the same thread replaces the
  working set; the conversation ending discards it.
- **Answer items get a stable shape** so "this one" can be resolved: each
  item carries an `id` the agent maps natural language onto ("the ramen
  place", "the second one"), so the product repo only echoes an id back.
  Ship the *full* item shape here — `id`, `kind`, `extent` — with `kind`
  always `venue` and `extent` always null for now, so Step 6 needs no second
  contract change and the app integrates once.
- Alternatives exclude everything already shown for that slot: asking twice
  never returns the same place.

**Constraints:** reorder is accepted silently in v1 — no "that adds 40km of
backtracking" commentary, even though reordering breaks the route-progress
sort Step 4 produces. Revision is free up to a per-answer cap (config value,
start at 5) and counts against consult quota beyond it (ADR-112) — every
revision is a real search, and uncapped free refinement turns one consult into
an unmetered search feed.

**Done when:** a user can take a stop out of a Da Nang→Hue journey, swap
another for an alternative, and add one in between — without re-asking, and
without anything being written to the database.

---

## Step 6 — Areas as recommendations

**Problem it closes:** problem 4 — legitimate area answers are impossible.

**Decided direction:**
- Consult can put an **area forward as the answer**, at any granularity:
  neighborhood ("where should I stay?"), city ("Hoi An or Hue?"),
  region/country ("where in November?"). Route-shaped asks ("scenic drive
  near Da Nang") get an agent-composed journey (the Step 4 shape) — never a
  stored route object. Kebi answers like a traveler who's local everywhere.
- The agent decides per query which kinds the curated list contains. **No
  contract change** — Step 5 already shipped `kind` and `extent`; this step
  starts populating them and lets areas win.
- Area ranking draws on the knowledge layer's claims + the taste model — the
  rich data accreted since Step 2.
- Rendering principles: an area renders as a shaded extent on the map — never
  a pin — with a one-line why; a composed journey renders as ordered venue
  stops along the way (there is no route card). No kind jargon or entity
  internals in the UI.

**Constraints:** area answers are recommendations, not doorways — the
zoom-in affordance and everything behind it is deferred (see Out of scope).
An area answer stands on its own or it isn't ready to ship.

**Check before planning this step:** claim coverage is not symmetric. Venue
claims have accreted since ADR-120; area claims only started with Step 2 and
only for areas that appeared in shares or research. Ranking neighborhoods
needs enough claims *per entity* to discriminate — "An Thuong vs Son Tra" is
a far finer judgement than "Da Nang vs Hue". Count claims per area entity by
hierarchy depth first; if the tail is thin, this step grows a backfill
(curator sweep over the areas users actually ask about) before ranking is
worth building. Measure at plan time, not before — claims accrete
continuously, so a count taken now describes a dataset that won't exist by
then.

**Done when:** "which neighborhood should I stay in?" returns a ranked area
answer rather than a hotel or a paragraph of prose.

---

## Out of scope (whole roadmap)

- Persisted trips/itineraries — decided no; Kebi is a decision engine.
- A second rich-area database — the knowledge layer owns rich data.
- Road-shape corridor routing — straight-line sampling is v1.
- Mixed-kind consult answers before Step 6.
- Trusting or persisting externally named routes — route names collapse to
  containing-area interest; journeys are agent-composed only.
- **Kind navigation — deferred, parked not killed (2026-08-02).** The
  conversation anchor (generalizing ADR-131's conversation-scoped research
  area across tools) and the three follow-up moves: **zoom in** (area →
  venues inside its extent), **zoom out** (venue → containing area, via the
  entity hierarchy), **zoom across** (sibling areas under the same parent,
  ranked by taste). Deferred because it carries most of the complexity of
  area answers — real extent-scoped retrieval, hierarchy traversal, sibling
  ranking — while areas-as-answers stands alone without it. Revisit after
  Step 6 ships and the logs show whether people try to navigate from an area
  answer.
- Library kind rendering — still demand-gated; nothing in this roadmap
  depends on it.
