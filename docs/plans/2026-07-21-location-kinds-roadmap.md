# Location Kinds — Roadmap

**Status:** direction decided 2026-07-21 · Step 1 done 2026-07-22 (ADR-133) · Step 2 done 2026-08-02 (ADR-134) · Step 3 done 2026-08-02 (ADR-135) · Steps 4–6 re-scoped 2026-08-02 · Step 4 done 2026-08-04 (ADR-136) · **Step 6 done 2026-08-06 (ADR-142), ahead of Step 5** · Step 5 is next up; Steps 7 and 8 runnable in parallel · **goal run defined 2026-08-06** (see The goal run — re-run at the end of every step)
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
10. **The agent's own knowledge is locked out of the answer** *(added
   2026-08-04, after Step 4 live-testing)*. Asked "trip from Da Nang to Hue",
   a plain LLM answers with Marble Mountains → Hai Van Pass (stop at the
   bunkers, go early before the haze) → Lang Co → Lap An Lagoon → Elephant
   Springs. Kebi returned one or two venues and apologised. Three causes, none
   of them retrieval: **(a)** every one of those stops *validates* in kebi's
   pipeline — they were never proposed, because naming is delegated to a
   small helper model while the orchestrator, which knows the road, is not
   allowed to name anything; **(b)** the agent prompt forbade it — "names you
   mention must come from a tool result", and timing/fee/safety tips "never
   from your own general knowledge"; **(c)** Google has almost no venue data
   on a mountain pass, so no amount of better search invents what isn't
   there. The value of that stretch is *experience knowledge*, which no layer
   was permitted to voice.

   **Direction:** the agent proposes what it knows and kebi searches around
   it — its names go to validation and come back as real cards, so knowledge
   and pins are one answer rather than two competing ones. A journey answer
   has three layers: the agent's knowledge as the spine, validated places as
   pins where they exist, and the user's saved places along the route. The
   line that keeps this honest: **prose may carry knowledge; a card must be
   validated** — plus never inventing operating facts (hours, prices, "open
   24h"), which are checkable and send people to locked doors. This narrows
   ADR-133 to what it was actually written against — a route *saved* as a
   venue, or *pinned* as a place card — both still blocked.

11. **A validated non-venue can be saved as a venue** *(added 2026-08-04 —
   opened by ADR-137, owned by no step)*. ADR-137 lets a place that validates
   become a card, which is what finally allowed Hai Van Pass to be offered as
   a stop. But a card carries a save action, and the save path cannot tell a
   mountain pass from a restaurant: the provider holds **two** records for
   that pass — one typed as natural geography, which ADR-133 correctly drops,
   and one typed `historical_landmark`, which is indistinguishable from any
   other venue. So a type-based guard is impossible, and ADR-133's guarantee
   ("no venue-typed non-venues in the library") currently rests on nobody
   tapping save.

   **Assigned to Step 6 (2026-08-04).** A save-time guard was investigated and
   abandoned — the geocoder types Hai Van Pass and Lang Co Beach identically as
   `natural_feature`, so any guard that blocks the pass also blocks beaches,
   lagoons and springs, which are among the best saves on that very route. The
   answer is the model, not a guard: a pass is geography with extent, so it
   should be an **area**, and then saving it is an area save rather than a
   venue row. **Closed 2026-08-06 (ADR-142)** — with one honest bound: the
   correction reads the entity store, so a non-venue kebi has never resolved
   is still offered as a venue. The guarantee strengthens as the store fills
   rather than holding on day one.

12. **A refused non-venue is a dead end — kebi never offers what's actually
   there** *(added 2026-08-04)*. Saving "Hanoi Train Street" is refused
   ("looks like a route or region — noted as a travel interest") and stops.
   But Train Street is a famous attraction: people sit at cafés on the track
   and have a beer waiting for the train to pass. Google has no attraction
   record for the street — only the alley, typed `route` — **while the cafés
   on it are all ordinary valid venues**: *Train track cafe*, *RAILWAY TUAN
   CAFE*, *Train Street Hanoi coffee*, *CAFE 61 TRAIN ST.* The thing the user
   actually wants is savable; kebi just never looks for it.

   So the refusal is unhelpful rather than wrong. Having identified where the
   named geography is, the next move is to offer the venues *at* it — "the
   street itself isn't a place to pin, but here's what's on it" — which is
   also the honest version of "never a silent drop".

   **Not covered by Step 6.** Step 6 makes the street an *area*, so it can be
   an answer and be saved as one. It does not make extraction look for venues
   at a refused name. The two compose (an area gives the extent to search
   inside) but neither depends on the other, and this one is not blocked.

13. **An answer is places without the conditions around them** *(added
   2026-08-04, comparing a Hanoi→Saigon answer against a general assistant)*.
   Kebi returned nine good stops with durations and transport costs. The
   assistant returned twelve plus the things that actually shape the trip:
   the central coast's **September–December rain**, the choice between
   Highway 1A and the emptier inland Ho Chi Minh Highway, that people rent a
   motorbike one-way Hanoi→Saigon, and what to **skip**. Kebi said none of it,
   and never tells anyone what isn't worth doing.

   The same shape showed up earlier on a Hanoi itinerary: the assistant named
   egg coffee and a water puppet show — things you *do* — while kebi listed
   only places you go.

   **Cause:** we ask the agent for places, so we get places. Conditions and
   experiences aren't attached to a pin, and nothing in the prompt or the
   pipeline reaches for them, even though ADR-137 already permits the agent to
   say them. Coverage suffers for the same reason — the prompt asks for stops
   along a route, never for the complete set, so the model stops at a good
   list. Compounding it, `route_too_long` suppresses **every** card on a
   long route, including venues the agent named correctly and which validate
   today (Cu Chi Tunnels, War Remnants Museum, Trang An) — a gate meant to
   stop *inventing* stops is also blocking verification of real ones.

   *Owned by Step 8.*

## Goal

**Kebi is a traveler who's local everywhere.** It answers at whatever
granularity the question demands — a café, a neighborhood, a city, a region, a
scenic route — each stored, ranked, and rendered as what it actually is.

**Kebi is a places engine, not a route solution.** It surfaces areas and venues
— near a point, on the way between two, or anywhere the question points. "On
the way" is a spatial filter like "near me", not a routing product: routing
belongs to maps apps, and what's worth stopping at belongs here. Everything
below follows from that.

**The agent suggests, and kebi searches around what it suggests.** The
orchestrator's own knowledge of the world is a first-class source, not a
fallback: it names the places and areas worth going to, and kebi verifies them,
places them, and pins them. Retrieval serves the agent's judgement rather than
replacing it — which is why an answer can be as good as a well-travelled
local's *and* carry real coordinates, saves, and taste behind it.

**This is the edge over a general assistant.** ChatGPT or Claude can lay out
the Da Nang→Hue road as well as kebi can — that stretch of the goal is table
stakes, not a moat. What they cannot do is what happens *around* the route:
pull the real places along it, verified and pinned with coordinates, hours and
a save action; layer the user's own saved places and taste over them; and carry
the insider knowledge kebi has accreted — claims harvested from shares,
research and curation, tied to the area rather than recalled from a training
set. A general assistant hands you prose you then have to go re-look-up. Kebi
hands you the same judgement already grounded in places you can act on.

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

## The goal run *(added 2026-08-06)*

One question, run at the end of every remaining step, against the real app:

> **"I'm doing Hanoi, then Hue, then Hoi An — what should I stop at?"**

The answer we are building toward, leg by leg:

- **Hanoi** — *Train Street*: the street itself isn't a pin, but the cafés on
  the track are ordinary venues and come back as cards, with the line about
  sitting there with a beer waiting for the train. Plus whatever the user has
  saved in the city.
- **Hue** — the user's own saved places surface first; suggestions fill in
  around them.
- **Hue → Hoi An** — the *Hai Van Pass* is offered as the stretch it is (an
  area, not a pin), carrying insider knowledge kebi has accreted about it —
  go early before the haze, stop at the bunkers — not recalled prose. Real
  places along the way come back validated: Lang Co, Lap An Lagoon, Elephant
  Springs.
- **Da Nang** — proposed although the user never named it, because it sits on
  the way and their saves and taste point at it. Kebi adds a stop the question
  didn't ask for and is right to.
- **Hoi An** — saved places plus suggested sites, closing the chain.

**Why this question:** it is the whole roadmap in one turn — a multi-stop chain
(Step 4), an area winning a slot in the answer (Step 6), venues *at* a named
geography that isn't itself savable (problem 12), knowledge deep enough to say
something a local would say (Step 7), and the conditions layer around the
places (Step 8). It is also the exact comparison a general assistant loses:
every one of those stops is pinned, saved, or savable.

**It makes problem 12 load-bearing.** Train Street is the Hanoi leg of this
answer and no step owns it today. Whichever step picks it up, the goal run
doesn't pass without it.

**How to use it:** at the end of each step, run it live and record what the
step moved — not pass/fail on the whole thing (early steps will fall short of
most of it), but which legs improved and which are still prose.

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
  corridor geometry). Step 6 depends on 5 for the item shape. **Step 7 is
  outside the chain** — no dependencies, runnable in parallel from now, and
  should start before Step 6 since claims accrete over time.
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

## Step 4 — Trip-shaped queries answer from data  *(done 2026-08-04 — ADR-136, `feature/corridor-search`)*

**Problem it closes:** problem 5 — corridor is prose-only.

**Shipped direction:**
- Corridor search is real geometry: waypoints sampled along the route, union
  of the existing disc searches, dedup, off-route filtering, and ordering by
  progress along the route. Both place tools share one geometry helper —
  `suggest_places` covers the whole route with a single coarse bound so its
  call count is unchanged, `discover_places` fans out across the sampled
  points under a config cap.
- The candidate namer receives route context, so proposals spread along the
  way instead of clustering at the origin.
- The answer stays within the existing one-list contract (ADR-091), ordered by
  route progress and narrated as a journey.

**Deviations (in ADR-136), each grounded:**
- **A route is an ordered chain, not one segment.** "Hanoi, then Hue, then Hoi
  An" is a normal way to describe a trip; a single-destination path is just a
  one-stop chain. Stops resolve all-or-nothing — silently dropping one answers
  a different question.
- **Scale is gated, per leg.** Venue stops are honest up to roughly a long
  day's drive; across a country the real stops are cities, which consult
  cannot return until Step 6. An over-long leg gets no interior sampling, and
  a trip with no answerable leg returns a distinct outcome that spends **no**
  model or provider call — the agent says the trip is city-scale and works out
  which stretch the user wants. That outcome is an answer, not a failure.
- **POI endpoints are handled directly** (the open question above): the
  geometry consumes coordinates only, so an endpoint with no `area_entities`
  row needs no degrade-to-containing-area.
- **Endpoint resolution reordered.** Qualifying a destination by the *origin*
  city — the previous single-destination behaviour — resolved "Saigon" from
  Hanoi to an unrelated address on Hanoi's edge, a 10 km route instead of
  1,100 km. Resolution is now store → country-scoped (settlement only) →
  locally-qualified.
- **Hard extent-scoping still did not land.** Corridor geometry turned out not
  to need it: the route's own half-width is the fence. Extent-scoping remains
  unbuilt and unclaimed.

**Constraints held:** straight-line waypoint sampling is v1 — road-shape
routing stays out of scope, and is what would make this a routing product
rather than a spatial filter. No new geo infrastructure; the existing
radius-search path is reused unchanged. The journey is composed at answer time
and never persisted.

**Rollout note:** config-only. No migration, no contract change, no cache
invalidation. Checkpointed conversations carrying the old single-destination
shape are coerced forward explicitly, so a turn in flight survives the deploy.

**Done when:** "trip from Da Nang to Hue" returns an ordered set of real,
validated stops along the route instead of one famous landmark. *(met —
verified live at all three scales: single leg, multi-stop chain, and a
country-length trip that declines to invent stops)*

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
  **The item shape already shipped** — Step 6 ran first and landed `id`,
  `kind`, `extent`, `area` and `anchor_area_key` on every candidate
  (ADR-142). Step 5 consumes `id`; no further contract change is needed.
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

## Step 6 — Areas as recommendations  *(done 2026-08-06 — ADR-142, `feature/area-answers`)*

**Problem it closes:** problems 4 and 11 — legitimate area answers were
impossible, and a validated non-venue could still be saved as a venue.

**Deviations from the brief, each grounded:**
- **Shipped before Step 5, and shipped Step 5's item shape.** The brief said
  "no contract change — Step 5 already shipped `kind` and `extent`". Step 5
  had not run, so this step ships `id` / `kind` / `extent` itself. The app
  integrates once and Step 5's revision work needs no second change, which
  was the brief's actual intent.
- **Extent decided as degrade-to-point.** No OSM dependency: an implausible
  bbox is disbelieved rather than corrected, and the area anchors on its
  centroid with a kind-derived radius. Kebi has no better geometry for a
  linear feature and will not invent one.
- **The save question resolved as signal-only.** Keeping an area emits
  region interest (`POST /v1/signal`, `signal_type: "area_saved"`) and writes
  no row, so library kind rendering stays demand-gated as decided.
- **One resolver engine, one spec per kind.** `resolve_country` /
  `resolve_city` / `resolve_area` are now three acceptance sets over a single
  resolution path, so the next kind is a spec entry rather than a fourth
  near-duplicate method.
- **The kind correction is store-dependent** (see Consequences in ADR-142) —
  a non-venue kebi has never resolved still comes back as a venue, so the
  guarantee strengthens as the store fills.

**Follow-up, same branch (ADR-144).** Live-testing this step found that the
area-anchor branch returned *before* the corridor check, so a trip turn that
named areas silently lost ADR-136's off-route filtering and route ordering —
a Da Nang → Hue ride came back with a park in Hoi An, the opposite direction.
Fixed by composition rather than precedence: the agent flags
`travel_between`, the named areas become the path, and the stretches between
them are searched too (gated per leg, per ADR-138). The same change assembles
the turn's answer once — flat items plus an ordered group index — so a saved
place and a suggestion in the same area render together.

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

**Also closes problem 11 — a named natural feature is an area, not a venue**
*(added 2026-08-04)*. Hai Van Pass ships today as a venue card because Google
types it `historical_landmark`, and it is savable as one. Guarding the save was
investigated and abandoned: the provider holds two records for it, and the
geocoder types the pass and Lang Co Beach identically as `natural_feature`, so
no signal separates "geography you drive through" from "a beach you go to".

The way out is not a guard but the right model — **a pass is geography with
extent, which is what an area is.** Made an area entity it renders as a shaded
extent rather than a pin, and the save problem dissolves, because saving it is
an area save and never a venue row. Two things block that today and belong in
this step: `AreaEntityType` carries only country/city/neighborhood, and the
resolver accepts settlement-level types only, so `natural_feature` refuses.

Note this needs geometry the provider will not give — Google returns a 0.4 km
bbox for a ~20 km road — so the extent has to come from elsewhere (OSM holds
the actual way) or the render degrades to a point. Decide that here.

**Areas are search anchors, not just cards** *(added 2026-08-04)*. This step
is governed by **ADR-140** — *the agent suggests, and kebi searches around what
it suggests*. That already holds for venues; here it holds for areas. The agent
names the areas worth going to, and kebi searches **anchored on each one** —
the user's saves, validated venues, discovery — rather than on a disc around
the turn's origin or on coordinates sampled off a chord between them.

So an area is an **input** to retrieval, not only an output of it, and the step
is designed around that first; the card shape follows. Reading it the other way
— resolve an area, rank it, render it — is what produces a trip answer that
names Hue, Hoi An and the pass in prose while pinning nothing at any of them.

Two consequences. Extent stops being cosmetic: searching inside an area needs a
real bounding box, so the viewport-versus-bounds problem below is load-bearing.
And this is **not** the deferred zoom-in affordance — zoom-in is a user action
on an answer already given; this is how the answer gets built.

**Constraints:** area answers are recommendations, not doorways — the
zoom-in affordance and everything behind it is deferred (see Out of scope).
An area answer stands on its own or it isn't ready to ship.

**Depends on data, not just code:** ranking areas needs enough claims per
entity to discriminate between them. That's Step 7's job — it has no
dependency on this step or any other and should be running before this one
starts.

**Done when:** "which neighborhood should I stay in?" returns a ranked area
answer rather than a hotel or a paragraph of prose.

---

## Step 7 — Area knowledge depth  *(no dependencies — runnable in parallel from now)*

**Problem it closes:** areas are identified but thinly known — Step 6 can rank
them only as well as the knowledge layer describes them.

**Why it stands alone:** Steps 4 → 5 → 6 are a chain; this one depends on
nothing in it and nothing depends on it except Step 6's answer *quality*. It
is numbered last but should start first — claims accrete over time, so
starting early is the whole point. Numbered 7 rather than inserted earlier to
keep existing step numbers stable.

**Why it compounds:** every turn names entities, and every named entity is a
chance to know it better — so usage itself builds the store, and the store is
what the agent pulls from on the next question. That is the third leg of
ADR-141's moat: a general assistant recalls a country from a training set,
while kebi reads back what it has accumulated about *that* city, *that*
neighborhood, *that* stop. It is also why this step is worth running long
before Step 6 needs it — a knowledge base is not something you build in a
sprint, it is something you have been collecting.

**Decided direction:**
- **Started in Step 6:** every turn that resolves areas now logs which of
  them kebi knows little or nothing about. That is the measurement below,
  taken from real questions rather than a speculative sweep — read those logs
  before scoping this step. Nothing else of Step 7 shipped: writing new
  claims needs a producer that does not exist yet, and that is this step.
- **Measure before building.** Count claims per area entity by hierarchy
  depth. Coverage is not symmetric: venue claims have accreted since ADR-120,
  area claims only since Step 2 and only for areas that happened to appear in
  shares or research. The count decides whether this step is an afternoon or
  a sustained effort — scope it from the number, not from a guess.
- **Depth matters more than breadth.** "Da Nang vs Hue" is an easy
  discrimination; "An Thuong vs Son Tra" is the real bar. Neighborhood-depth
  entities are where thin coverage will hurt, so measure and enrich by depth
  rather than by entity count.
- **Enrich through the existing curator**, targeting the areas users actually
  ask about (research and consult logs), not a speculative sweep of world
  geography.
- **Every mention is a harvest trigger.** A turn that names Hanoi, then Hue,
  then Hoi An has just told kebi exactly which three entities are worth
  knowing better — and the same holds for a route's containing area, a
  corridor's endpoints and waypoints, and the venues that come back as stops.
  Rather than mining logs after the fact, resolution itself queues enrichment:
  whenever an entity is resolved in a turn, it is marked for a knowledge pass
  if its claims are thin or stale. Enrichment runs in the background and never
  blocks the answer — this turn's user does not wait for it, the next one
  benefits.
- **Harvest up the hierarchy, not just at the mention.** Naming Hoi An is also
  a signal about Quang Nam and about Vietnam: country- and region-level claims
  (when to go, how people move between cities, what the food is about) are what
  make a multi-city answer read as informed, and they are cheap because one
  pass serves every entity beneath them. Walk `parent_key` upward from each
  mentioned entity and fill thin ancestors too, with the depth-first priority
  above deciding what gets the effort.
- **Venues harvest the same way.** A validated stop is an entity with claims
  like any area — the tips that make a place worth stopping at (go early, sit
  upstairs, skip the set menu) accrete on the venue, so the mechanism is one
  mechanism, not an area-only one.

**Constraints:** no new storage and no second rich-area database — claims land
in `knowledge_claims` against the existing geo-slug entity keys, exactly as
today. Background-only: nothing here is user-visible or touches the product
repo contract.

**Done when:** the areas users ask about most carry enough claims to
distinguish neighbors from each other, and a spot-check of ranked candidates
reads as informed rather than generic.

---

## Step 8 — A trip answer is places *and* conditions  *(no dependencies)*

**Problem it closes:** problem 13 — kebi asks the agent for places, so it gets
places. A trip answer needs the layer around them.

**Decided direction:**
- **Conditions become part of a trip answer**: when to go and when not to
  (the central coast's September–December rain is the single most
  decision-changing fact in a Hanoi→Saigon answer, and kebi omitted it),
  which road (Highway 1A versus the emptier inland Ho Chi Minh Highway), how
  people actually do it (one-way motorbike rentals Hanoi→Saigon), and what to
  **skip** — kebi never tells anyone what isn't worth it.
- **Experiences count as answer items too** — egg coffee in Hanoi, a water
  puppet show, a beer on the track at Train Street. Things you *do*, which
  the venue-shaped pipeline never reaches for even though the prose layer is
  free to name them (ADR-137).
- **Ask for completeness on a trip.** A 1,700 km route answer returned 9 stops
  where a general assistant gave 12, missing Da Lat, Vung Tau and My Son. The
  prompt asks for stops along a route and never asks for the full set, so the
  model stops at a good list rather than a complete one.
- **A country-scale answer still pins what it can.** `route_too_long`
  currently suppresses every card, including venues the agent named correctly
  and that validate today — Cu Chi Tunnels, War Remnants Museum, Trang An.
  The gate exists to stop *inventing* stops across a country, not to stop
  verifying real ones. Sampling stays off; validation of agent-named places
  turns back on.

**Constraints:** conditions are knowledge, not a data feed — no weather API,
no live prices, no booking (ADR-139). Seasonality and road character come from
the agent's own knowledge under ADR-137's line: prose may carry it, a card
must still be validated, and operating facts stay tool-only. The durable home
for this is the knowledge layer — a claim that the central coast floods in
October belongs against the area, which is Step 7's substrate.

**Also fix here (small, unrelated to the above):** the agent leaked tool
mechanics into a user-visible answer — *"the tool confirmed it's too long"* —
despite the movement context instructing it to phrase this as an observation
about the trip. The instruction exists and did not hold.

**Done when:** a long-route answer says when to go, which way, and what to
skip alongside where to stop — and the stops that can be verified still come
back as cards.

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
