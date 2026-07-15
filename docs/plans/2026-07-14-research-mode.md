# Plan: Research mode — insider answers from the knowledge layer

## Context

Kebi today only recommends from places the user already saved, and when asked
anything outside "recommend a saved place" it deflects (asked about the World
Cup it says "not my field"; asked about Da Nang it answered about Koh Samui).
Two things are missing:

1. **A reader over the knowledge layer.** `knowledge_claims` has had writers
   since ADR-121 (harvest, curation, kebi-notes) and one reader since ADR-127
   (Library insider notes), but nothing lets the *agent* pull insider knowledge
   to answer a research question mid-conversation. The repository read methods
   already reserve `approved_only=True` "for the future research tool" — this is
   that tool.
2. **Grounded entity resolution + graceful engagement.** The Da Nang→Koh Samui
   bug is an entity-resolution failure: Kebi answered about a different place
   than the one asked about. And the hard "I only find places" deflection is
   purely prompt-driven and too rigid.

**Intended outcome:** the user can ask an insider/research question ("low-fee
ATM in Da Nang", "what should I order at X", "how's the coffee scene in Y") and
get an answer drawn from the knowledge layer — from someone who already knows
the scene. When the asked-about entity can't be confidently resolved, or the
layer has no claims yet, Kebi asks a clarifying question instead of pushing away
or silently swapping to a different place. Non-place questions (World Cup) are
engaged directly. Recommendation mode is untouched.

### Locked design decisions (from user)

- **Routing:** a new `research` tool added to the consult-family; the
  orchestrator picks it via the prompt (no new graph node, no `mode` state).
- **Entity resolution:** a **dedicated staged resolver** with a confidence
  score — stage 1 exact match, stage 2 `EntityGeoResolver` round-trip verify,
  stage 3 low/ambiguous → clarify. Never substitutes a different entity.
- **Non-place topics:** answer directly from world knowledge (widen the
  persona, drop the hard deflection). Never "not my field."
- **No claims found:** ask a clarifying question — never fabricate a tip, never
  fall through to generic place discovery.
- **Density (D1):** ship as-is; research is honest (clarify when empty) and fills
  in as harvest/curation populate the layer. No launch-content work.
- **Scope (V1):** research is **area-only** (country / city / neighborhood).
  **No** per-venue `place:<id>` path in v1 — a venue question resolves to its
  area or clarifies. Venue research is deferred entirely.
- **Tag quality (H1 + C):** this task **also** touches the write side. A new
  **controlled claim-tag vocabulary** (option C) is introduced — the applicable
  places tag types **plus** a new practical/area set (money/fees, payment,
  safety, transport, etiquette, timing-trick) the places vocab lacks. The
  **harvester is retuned** to capture practical insider facts and emit tags from
  that vocabulary, so research has reliable tags to match and richer facts to
  read. Research (read) and harvest (write) share the one vocabulary.
- **Cross-country resolution:** when the question names an area outside
  `working_location`'s country, the resolver tries the **agent-passed `country`
  first** (`resolve_country` it, then constrain `resolve_city` to that code),
  falls back to `working_location`'s country, and clarifies only if both fail —
  so "tell me about Da Nang" answers even from a stale Thai working_location.
- **Country scope descends:** a country question reads `list_under_prefix`
  (country + its cities/neighborhoods) like city scope does; Stage C's proximity
  penalty + cap keep it from swamping. (Strict country-key-only would return
  `no_claims` for almost every country question, since ADR-124 files
  towns/provinces as *city*-scoped.)
- **Home recall:** research-only turns are **excluded** from the ADR-110
  `user_intents` write — `surfaced_places` counts only place-tool results.
- **No tag backfill:** existing free-form claim tags are not migrated; old
  claims rank via text/trust/proximity only (ADR-125 orphan-don't-migrate
  precedent).

---

## Scope — use cases covered & explicit gaps

### Use cases covered (v1)

1. **Insider facts about an area** (country / city / neighborhood): what to order,
   when to go, local tricks, practical tips (low-fee ATM, tipping, safety, how to
   pay), scene overviews. The core case.
2. **Da Nang ≠ Koh Samui** — a named area resolves to *that* area, verified, or it
   asks; never a nearby/stale substitute.
3. **Compound: research informs recommendation** — the strongest emergent case,
   free from having both tools in one turn. "Where do I get cash without getting
   killed on fees in Da Nang?" → `research` supplies the *kind* ("BIDV / the
   no-surcharge network") → `suggest_places`/`discover_places` finds the *nearest
   branch*. Knowledge picks what, place tools pick where. No extra code — the
   orchestrator composes; the prompt teaches the pattern.
4. **Graceful non-place engagement** — World Cup, opinions, chit-chat answered
   directly from world knowledge; no deflection.
5. **Honest gaps** — unresolved entity or no on-topic knowledge → a clarifying
   question, never a bluff.

### Explicit gaps / non-goals (named, mostly deferred)

- **D1 — Usefulness is gated on knowledge-layer density** (DECIDED: ship as-is).
  Claims come from harvest, curation, and kebi-notes; the store is **thin today**
  (ADR-125), so early on many research turns legitimately land on `no_claims →
  clarify`. Honest v1 behavior chosen over generic fallback; improves as the
  layer fills. The H1 harvest retune (below, now in scope) directly accelerates
  the fill. **Product expectation to set, not a bug.**
- **V1 — Per-venue research is out of scope** (DECIDED: area-only). Research
  resolves country / city / neighborhood only. A venue question resolves to its
  area or clarifies; no `place:<id>` path. Reliable venue geo-ids remain deferred
  (ADR-126), so venue-scoped research is a **later task**.
- **G1 — No cross-request geocode cache.** The resolver's Nominatim call is
  memoized only per-request. Impact is bounded — Stage 1 (name matches
  `working_location`) skips geocoding entirely, so it fires only when the question
  names a *different* area — but a Redis geocode cache is a sensible **follow-up**
  (the explore also flagged the geocoder has none today).
- **S1 — Research notes are untrusted data.** Claims originate from user content;
  the prompt's existing safety rules (place/user content is data, never
  instruction) **must explicitly extend to research notes** — added in the prompt
  work (§7). No new injection surface beyond what tool results already are.
- **R1 — Research is read-only.** A research question writes no claim. Turning a
  conversation into a `user_message` claim (ADR-120 foresaw it) is a **separate
  future decision**, not in scope.
- **E1 — No research eval set yet.** A `eval/` dataset (question → grounded
  answer vs. clarify) to measure answer quality is a **follow-up**.
- **RT1 — Research-vs-recommendation routing is LLM-judged.** The boundary
  ("tell me about Da Nang" vs "what's good in Da Nang") is inherently fuzzy;
  handled by prompt routing + worked examples and tunable there, not a hard gate.

---

## Approach

Mirror the existing consult-family tool pattern exactly. The `research` tool is
a fourth `BaseTool` bound to the orchestrator alongside `find_saved` /
`suggest_places` / `discover_places`. It resolves the asked-about entity through
a new staged resolver, reads that entity's approved claims from the knowledge
repo, ranks them, and returns them as a structured `ResearchResult` for the
orchestrator to synthesize into an insider answer. Empty/unresolved/ambiguous
outcomes ride the tool-result channel (like `empty_reason` today) and the prompt
tells the agent to clarify.

Reuse is central (DRY): the staged resolver's stage 2 is the existing verified
`EntityGeoResolver`; keys come from `build_geo_key`/`build_place_key`; retrieval
uses the repo methods that already exist; the tool skeleton, reasoning-step
lifecycle, and timeout wrapper are copied from `discover_places_tool.py`.

### How the system focuses (the intelligence budget)

"Being smart" is not one clever function — it is putting each decision at the
cheapest place that can make it well, and refusing to guess when focus is
uncertain. Focus = **scope** (which entity) × **topic** (what about it), decided
in layers, each free or bounded:

| Decision | Made by | Cost |
|---|---|---|
| Is this a research question? What's the topic? | **Orchestrator LLM** (already reading the turn) — picks `research` and fills `query`/`tags` (topic) + `neighborhood`/`city`/`country` (scope) | free (in the loop) |
| Which scope (country/city/nb)? Is the entity *real*? | **Staged resolver** (code) — verified geo key + `entity_type` + confidence; geocode only when the name ≠ `working_location`, memoized | ~0, occasional free Nominatim call |
| Which claims, within scope? | **Exact-key read** + hierarchy inheritance (indexed) → **in-memory tag/text/trust/proximity rank** (pure fn) | 1 indexed read, no model call |
| Does any note actually answer it? | **Orchestrator LLM** — answer only from returned notes, else clarify | free (in the loop) |

Governing principle (the codebase's own split, ADR-121: "a model can mis-word a
claim but never mis-scope it — keys come from resolved data"): **let the LLM do
the fuzzy work (intent, topic, final wording); keep the exact/verifiable work
(entity identity, keys, reads) in deterministic code. Bound before you rank;
rank before you synthesize. When focus is uncertain — a shaky entity or no
on-topic claim — clarify, never broaden.** That discipline is what makes it read
like a local who knows the scene rather than a search box.

---

## Changes

### 1. New — staged entity resolver `ResearchEntityResolver`
**File:** `src/kebi/core/knowledge/research_resolver.py`

A dedicated resolver, **geo-only** (country / city / neighborhood — no venue
path, per V1): given the asked-about entity (agent-passed
`city`/`country`/`neighborhood`, else the turn's `working_location`) plus the
turn's `working_location` for country context, produce a `ResolvedEntity`
(entity_key, entity_type, entity_name, `confidence: float`,
`needs_clarification: bool`, `clarification_reason: str`).

**Prereq — `WorkingLocation` gains `country_code`.** `WorkingLocation`
(`core/agent/location.py:121`) carries `country` as a **display name**
("Vietnam"), no ISO code — but `build_geo_key` regex-validates alpha-2 and
`resolve_city` requires a code. The Nominatim reverse result **already returns**
`country_code` (`core/places/nominatim_geocoding_client.py:52`); `graph.py`
just drops it at the two `WorkingLocation(...)` construction sites (~984,
~1007). Add `country_code: str | None = None` to the model (it is
`extra="forbid"`; the `None` default keeps previously checkpointed states
valid) and thread it through both sites. When absent (old checkpoint), the
resolver derives it via memoized `resolve_country(working_location.country)`.

Stages:

- **Stage 1 — exact match (confidence 1.0):** if the named area slug-matches the
  turn's already-resolved `working_location` (via `slugs_match`), build the key
  from `working_location.country_code` (see prereq) + city/neighborhood with
  `build_geo_key`. This reuses ADR-124's authoritative resolution for the common
  single-place turn. **Crucially, if the named area does NOT slug-match
  `working_location`, do not use it** — fall to stage 2 (this is what closes the
  Da Nang→Koh Samui swap).
- **Stage 2 — verified geocode (confidence ~0.8):** resolve the named entity via
  `EntityGeoResolver.resolve_city(name, country_code)` or
  `resolve_country(name)`. The constraining country code is tried in order:
  **agent-passed `country` first** (resolve it via `resolve_country` — the
  orchestrator usually knows Da Nang is in Vietnam), then
  `working_location`'s code. Accept only the round-trip-verified result
  (returns `None` on unverifiable). Build the geo key from the verified
  `ResolvedGeo`. (ADR-126's "constrained to the anchor's country" rule carries
  over with the agent-passed country playing the anchor role; a stale
  working_location country alone would wrongly clarify the headline
  Da-Nang-from-Koh-Samui case.)
- **Stage 3 — clarify:** unresolved, unverifiable, or ambiguous → `confidence`
  below `entity_confidence_min` and `needs_clarification=True` with a plain-
  language reason. **Never returns a key for a different entity.** A question
  about a specific *venue* has no `place:<id>` path in v1: resolve to the venue's
  area if one is named, else clarify.

Reuses: `slugs_match` (`core/knowledge/geo_resolve.py:29`), `_slugify`,
`build_geo_key`, `ResolvedGeo` (`core/knowledge/schemas.py`),
`EntityGeoResolver` (`core/knowledge/geo_resolve.py`), `WorkingLocation`
(`core/agent/location.py`).

### 2. New — result/output models `ResearchResult`, `ResearchNote`
**File:** `src/kebi/core/knowledge/research_models.py`

- `ResearchNote`: `id`, `text`, `tags: list[str]` (from the §3a vocabulary),
  `source` (coarse label community/expert/kebi), `confidence: float`,
  `agree_count`, `disagree_count`. **Label mapping caveat:** the Library path
  applies source_type→label at the **API layer** (`_NOTE_SOURCE_LABELS`,
  `api/schemas/library.py:36`), not in `PlaceNotesService` — but research's
  payload serializes into the `ToolMessage` and rides `tool_results` to the
  wire **generically**, so the coarse label must be applied **in the
  service/tool before serialization** or raw provenance leaks (ADR-127 forbids
  that). Extract `_NOTE_SOURCE_LABELS` to a shared spot (per DRY) and use it in
  `ResearchService`.
- `ResearchResult`: `entity_name: str | None`, `entity_key: str | None`,
  `notes: list[ResearchNote]`, `empty_reason: Literal["unresolved","ambiguous",
  "no_claims","no_topic_match"] | None`, `clarification: str | None`.
  Serialized to the `ToolMessage` content exactly like `ConsultResult`.

### 3. New — reader service `ResearchService` (the retrieval funnel)
**File:** `src/kebi/core/knowledge/research_service.py`

Modeled on `PlaceNotesService` (`core/knowledge/place_notes_service.py`). Holds
`repo: KnowledgeClaimRepository`, `resolver: ResearchEntityResolver`, and config
(`notes_limit`, ranking weights, `topic_relevance_floor`). One method
`async def research(*, query, tags, working_location, city, country,
neighborhood, user_id, limit) -> ResearchResult` runs a **four-stage funnel** —
each stage narrows cheaply before the next, and no stage does a store-wide
search.

**Stage A — Resolve entity (never wrong-entity).** Staged resolver (§1).
`needs_clarification` → `ResearchResult(empty_reason="unresolved"|"ambiguous",
clarification=reason)` and stop. This is the only place a place is named; a
low/ambiguous confidence never proceeds to retrieval.

**Stage B — Entity-bounded read (exact keys only, ADR-120).** Research targets
the three geo scopes — `neighborhood`, `city`, `country` (the store also has a
`place` scope, unused by research in v1) — and geo keys are hierarchical
(`vn` ⊃ `vn/da-nang` ⊃ `vn/da-nang/my-khe`). Retrieval targets **whichever scope
the question resolved to** and **inherits up** (a broader entity's claims are
reachable, ADR-120); descendant claims are pulled **ranked-and-capped** (Stage
C's proximity penalty), never dumped raw. Per-scope read rule:
- **neighborhood** (`vn/da-nang/my-khe`) → its own key + ancestors
  `list_for_entities(["vn/da-nang/my-khe", "vn/da-nang", "vn"])`. (Leaf — no
  descendants.)
- **city** (`vn/da-nang`) → `list_under_prefix("vn/da-nang")` (the city **and
  its neighborhoods** — descendants roll up, a "things in Da Nang" question wants
  My Khe notes too) **plus** ancestor `list_for_entities(["vn"])`.
- **country** (`vn`) → `list_under_prefix("vn")` — the country **and its
  cities/neighborhoods** (DECIDED: descend). ADR-124 files towns, islands, and
  provinces as *city*-scoped claims, so a strict country-key read would return
  `no_claims` for almost every country question while the store knows plenty.
  Stage C's proximity penalty ranks country-level ambient first and
  `notes_limit` caps the volume — the swamp is handled by ranking, not by
  refusing to read.
- Every read passes `approved_only=True` (ADR-122) and `user_id` (global + the
  caller's own claims only). All hit the `(entity_type, entity_key)` index — a
  bounded prefix scan and/or a small `IN`, never a table scan or ANN.
- Track each claim's **proximity** (own entity = 0, parent = 1, grandparent = 2)
  so Stage C ranks the specific over the ambient.

**Stage C — In-memory relevance rank (a prioritizer, not a store search).** Over
the already-entity-bounded candidate set, score each claim with a pure,
config-weighted function (same shape as `compute_confidence` — no I/O, testable
inline):
`score = w_tag·tag_overlap(agent tags + query tokens, claim.tags)
       + w_text·text_overlap(query tokens, claim.claim)
       + w_trust·claim.confidence
       − w_prox·proximity` (+ ADR-128 agree/disagree tally later).
Tags carry the most weight — they are the **controlled claim-tag vocabulary**
(§3a), so the agent's `tags` arg and a claim's `tags` are drawn from the *same*
list and match exactly (not fuzzy). Sort desc, cap at `notes_limit`. This is
client-side reranking of a bounded set, not the banned semantic search over the
store.

**Stage D — Honest empties + grounded hand-off.**
- 0 candidates under the entity → `empty_reason="no_claims"` (nothing known here
  yet) + a clarifying reason.
- Candidates exist but the top score is below `topic_relevance_floor` →
  `empty_reason="no_topic_match"` (know the place, not this angle) + a clarifying
  reason. Distinct wording from `no_claims`.
- Otherwise map the top-N to `ResearchNote`s and return them. The **orchestrator
  is the final relevance judge** (handles reworded claims lexical overlap
  misses), but the prompt constrains it to answer *only* from these notes and to
  clarify if none actually address the question — so the floor + the prompt
  together keep research grounded, never a stretched or generic answer.

**Why this is the smart shape, not a dump:** it bounds the DB read to one indexed
prefix per entity (cheap, ADR-120-clean), spends **zero** extra model/embedding
calls per turn (the orchestrator already in the loop does the semantic judgment
for free — aligns with `feedback_cost_opt_no_feature_loss`), leans on the
curated tag layer for selection, is trust- and proximity-weighted, and degrades
to a clarifying question rather than fabricating. **Scale path (future ADR):**
when a single entity's approved-claim count outgrows `notes_limit` enough that
lexical selection drops relevant claims, swap Stage C's lexical score for an
**in-memory embedding rerank** of the entity-bounded set (still not a store
search) — no change to the read primitive or the tool contract.

### 3a. New — controlled claim-tag vocabulary (option C)
**File:** `src/kebi/core/knowledge/tags.py` (mirrors `core/places/tags.py`)

Claim tags today are free-form (harvester emits arbitrary lowercase words), so
research can't match on them reliably. Introduce a **controlled vocabulary for
claim tags** that both the harvester (writes) and research (matches) share:

- **Reused from places** where it already fits a *fact about a place/area*:
  `cuisine`, `atmosphere`, `price`, `time`, `season`, and the relevant
  `feature` values. Import the existing enums from `core/places/tags.py` so the
  string values can't drift (no duplication).
- **New practical/area types** the places vocab lacks — the insider facts H1 is
  meant to capture: `money` (e.g. `no_fee_atm`, `cash_only`, `card_ok`,
  `cheap_eats`, `pricey_area`), `safety` (`safe_at_night`, `pickpockets`,
  `common_scam`), `transport` (`walkable`, `hard_to_reach`, `rideshare`,
  `metro`), `etiquette` (`tipping`, `dress_code`, `reservation_needed`,
  `remove_shoes`), `timing_trick` (`go_early`, `avoid_weekends`, `sunset_spot`,
  `long_queue`). (Exact values finalized in code; the point is a *bounded,
  documented* set.)
- **Accessibility stays categorically excluded** (ADR-118) — not in this vocab,
  and the writer already drops it.

The vocabulary is the single source both sides read: the agent passes these tag
values on the `research` arg, the harvester/curator emit them, and Stage C
matches value-for-value.

**No backfill (DECIDED):** existing claims carry free-form tags from the
open-ended prompt era and are **not** retagged — they still surface via Stage
C's text/trust/proximity terms, just without the tag boost. The store is thin
(ADR-125) and refills under the retuned harvester; ADR-125's
orphan-don't-migrate precedent applies.

### 3b. Harvester enrichment (H1)
**Files:** `config/prompts/knowledge_harvester.txt`, `core/knowledge/harvester.py`
(+ `curator.py`/`knowledge_curator.txt` for parity)

The harvester emits full-sentence claims but its prompt steers toward
*descriptive* facts (character, price, best-time) and its `tags` are free-form.
Retune so it also mines **practical/actionable insider facts** — payment/fees,
safety, transport, etiquette, timing tricks — and emits `tags` **from the §3a
vocabulary**:

- Prompt: add the practical-fact classes with examples; instruct that `tags`
  come from the controlled list; keep the existing accessibility ban and the
  "durable, grounded, no-invention" rules.
- Writer/normalization: normalize emitted tags to the vocabulary (keep known,
  drop unknown) so an off-vocab hallucination can't pollute the tag index —
  mirrors the writer's existing drop-don't-mis-key discipline (ADR-126).
- Same light touch on the curator so curated claims tag consistently.

This is the only write-side change; it makes the layer fill with material
research can actually use (accelerates D1), and it's config/prompt-weighted so
the tag set can be tuned without a release.

### 4. New — the `research` agent tool
**File:** `src/kebi/core/agent/tools/research_tool.py`

Copy the `discover_places_tool.py` skeleton: `_TOOL_NAME = "research"`;
`build_research_tool(research_service) -> BaseTool` factory with an inner
`@tool` using `InjectedToolCallId` + `InjectedState`; wrapped in `with_timeout`;
single user-visible reasoning row via `emit_step_active`/`emit_step_done` +
`tool_step_base_id`; returns a `Command` with the `ToolMessage`, appended
`reasoning_steps`, and `tool_calls_used + 1`. Reads `working_location` off
state, passes args + `state["user_id"]` into `ResearchService.research(...)`.

Deviations from a blind copy:

- **Args are a tailored subset, not the shared seven.** The consult trio's
  shared shape is actually query + **categories** + tags + neighborhood + city +
  country + limit; research takes `query`, `tags`, `neighborhood`, `city`,
  `country`, `limit` (no `categories` — that's a place-search concept) with
  **its own descriptions**: `_search_args.TAGS_DESC` describes *place* tags and
  would misguide the model — research's `tags` description must point at the
  §3a claim vocabulary.
- **`_maybe_working_location` is module-private** to `discover_places_tool.py`
  (lines 97-106). Extract it to a shared tools module (per DRY feedback) rather
  than importing a private name or duplicating it.
- **Timeout degraded shape:** `_with_timeout.py:68-74` hand-builds
  ConsultResult-shaped JSON (`{"candidates": [], "empty_reason": "error"}`), so
  a research timeout would emit `candidates` instead of `notes`. Make the
  degraded payload tool-aware (research → `{"notes": [], "empty_reason":
  "error"}`); the prompt reads `empty_reason` either way, but tests and clients
  shouldn't see a foreign shape.

### 5. Registration + wiring
- **`src/kebi/core/agent/tools/__init__.py`** — append
  `build_research_tool(research_service)` to `build_tools(...)` **unconditionally**
  (ungated; knowledge read is cheap, not behind `discovery_enabled`). Add the
  `research_service` parameter.
- **`src/kebi/api/deps.py`** `get_agent_graph` (line 1025) — construct
  `ResearchService` (repo via `get_knowledge_claim_repository()`, resolver via
  `EntityGeoResolver(get_geocoding_client())` — the resolver takes **only** the
  geocoder (`geo_resolve.py:47`), limit/threshold from config) and pass it into
  `build_tools`. Mirror how `hybrid_search` / `candidate_namer` are threaded.
  Consider a small `get_research_service(...)` dependency mirroring
  `get_place_notes_service` (deps.py:916).
- **`src/kebi/core/agent/tools/_summaries.py`** — add a `research` entry to
  `TITLES` (a short bold action line, e.g. "digging up the local intel").
  (`_with_timeout.py:90` falls back to a generic "searched" if missing — add
  the entry so the degraded row reads right too.)
- **`src/kebi/core/chat/service.py:185` — `surfaced_places` filter (DECIDED).**
  `TurnCompleted.surfaced_places` is currently `bool(tool_results)`; the
  research payload lands in `tool_results`, so a research-only turn would
  silently enter the ADR-110 home-recall list ("low-fee ATM in Da Nang" as a
  "what you wanted" chip). Count **only place-tool results by tool name**
  (`find_saved` / `suggest_places` / `discover_places`) so research turns never
  write `user_intents`. Covered by a test (§9).

### 6. Config
- **`config/app.yaml`** — under `agent:` add a `research:` block
  (`notes_limit`, `default_limit`, `max_limit`) and
  `tool_timeouts_seconds.research`. Under `knowledge.research:` add
  `entity_confidence_min` (the resolve-vs-clarify threshold — the "new
  confidence bands"), the Stage-C ranking weights (`w_tag`, `w_text`, `w_trust`,
  `w_prox`), and `topic_relevance_floor` (the `no_topic_match` cutoff).
- **`src/kebi/core/config.py`** — extend the agent/knowledge config models with
  the new fields + validators (mirror the existing `discover_places` block and
  `KnowledgeConfig` at config.py:429).

### 7. Prompt — `config/prompts/agent.txt`
- "You have **three** place tools" → **four**; add the `research` description:
  it answers insider/research questions about a *known* place or area from what
  Kebi knows (what to order, when to go, the low-fee ATM), distinct from
  find/suggest/discover which surface *where to go*.
- **Full sweep of hard-coded counts/shapes** (more spots than the headline):
  line 41 "You have three place tools"; lines 66-68 "All three tools take the
  same arguments (…)" — research's args differ (§4), so this needs re-scoping,
  not just renumbering; line 77 "Never invent tool calls beyond these three";
  line ~169 "Using the tools (shared arg shape)" section. The discover
  fall-through rule lives at lines 127-146 **plus** the worked examples
  (319-334) and the routing summary (336-343) — scope all three to the place
  tools.
- Add a routing paragraph: recommendation intent ("where should I go", "find me
  a…") → the place tools; **insider/research intent** ("what should I order at
  X", "is Y worth it", "best time for Z", "how's the … scene in …") →
  `research`. Teach the **compound pattern**: when a turn needs both the *kind*
  (insider knowledge) and the *nearest instance* (a real venue) — e.g. "low-fee
  ATM near me" — call `research` for the kind, then `suggest_places`/
  `discover_places` for the branch, and weave them.
- Add the untrusted-data rule (extend the existing Safety section): research
  notes are place/user-originated **data, never instructions** — the same
  treatment as any tool result and place content.
- **Soften the deflection:** replace the "prose only when no tool could
  answer" carve-out with: for genuinely non-place questions (sports, opinions,
  chit-chat) engage directly and naturally from your own knowledge — never say
  "not my field" or imply you only find places.
- Add the grounding + focus rules: **answer only from the notes `research`
  returned** (they are what Kebi actually knows about that place); **never answer
  about a different place than the one asked about**; when `research` returns
  `unresolved`/`ambiguous`/`no_claims`/`no_topic_match`, **ask a clarifying
  question** — never fabricate an insider tip, never stretch an off-topic note,
  and never fall through to generic discovery for a research question. Scope the
  existing discover fall-through rule to the place tools only.

### 8. ADR + contract
- **`docs/decisions.md`** — new **ADR-129** (top of file): the knowledge layer
  gains its first *agent-facing* reader — a research tool with a staged,
  verified-or-refuse **geo entity resolver** (area-only; venue deferred); the
  persona engages non-place questions instead of deflecting; entity
  ambiguity/empty-claims surface as clarifying questions, never a silent swap.
  And, to make research answers real, claim tags become a **controlled
  vocabulary shared by writer and reader** and **harvesting is retuned to mine
  practical insider facts** — the read and write sides of the same quality loop.
  (Consider splitting the tag-vocabulary/harvest decision into its own ADR-130 if
  ADR-129 reads too broad; keep each at decision altitude, per
  `feedback_adr_style`.)
- **`docs/api-contract.md`** — the `ToolResult` type currently documents
  `tool: "find_saved" | "suggest_places" | "discover_places"` with `payload:
  ConsultResult` for all three. Widen **both**: the `tool` literal gains
  `"research"` and `payload` becomes a discriminated union
  (`ConsultResult | ResearchResult` keyed on `tool`). Additive for clients —
  those that don't render it still get the agent's prose answer. Also note the
  ADR-110 recall change: research-only turns no longer count as
  place-surfacing (§5).

### 9. Tests — `tests/core/knowledge/` + `tests/core/agent/`
- `ResearchEntityResolver`: exact-match uses working_location; a named area that
  mismatches working_location is resolved independently and **never** returns
  the working_location key (the Da Nang≠Koh Samui case); **cross-country: an
  agent-passed country wins over working_location's** (Da Nang + country
  "Vietnam" resolves from a Thai working_location); working_location without
  `country_code` (old checkpoint) falls back to `resolve_country`; unverifiable
  name → clarify; verified city → geo key.
- `ResearchService` funnel: per-scope reads (country **descends** under its
  prefix with proximity rank + cap, city includes neighborhood descendants,
  neighborhood inherits ancestors),
  `approved_only=True`, `user_id` scoping; Stage-C rank orders on-topic +
  high-trust + proximity above ambient and caps at `notes_limit`; 0 candidates →
  `no_claims`, on-scope-but-off-topic (top score below floor) → `no_topic_match`;
  both carry a clarification reason.
- `ChatService`: a research-only turn dispatches `TurnCompleted` with
  `surfaced_places=False` (no `user_intents` write); a place-tool turn still
  sets it `True`.
- Claim-tag vocabulary (§3a): the practical types exist, values are stable, and
  tag normalization keeps known / drops unknown.
- Harvester (§3b): a content fixture with a practical fact ("cash only", "no-fee
  ATM") yields a claim tagged from the vocabulary; off-vocab tags are dropped;
  the accessibility ban still holds.
- `research` tool: Command shape, reasoning-step lifecycle, empty_reason
  plumbing (mirror existing `tests/.../discover_places` tool tests).

---

## Verification (end-to-end — per `feedback_verify_end_to_end`)

1. `poetry run pytest tests/core/knowledge tests/core/agent -q` — new + existing
   pass.
2. `poetry run ruff check src/ tests/` and `poetry run mypy src/` clean.
3. Drive the real app (`docker compose up -d`, `poetry run uvicorn
   kebi.api.main:app --reload`) and hit `POST /v1/chat` (Bruno collection at
   `kebi-config/bruno/` — add a `.bru`) with the four real-failure cases:
   - **World Cup** → engages from world knowledge, no "not my field", no
     research tool call.
   - **Da Nang** (seed a Da Nang claim; keep a stale Koh Samui `working_location`
     from a prior turn) → answers about Da Nang, `research` resolves the Da Nang
     key, **never** Koh Samui.
   - **Entity with no claims** → clarifying question, no fabricated tip.
   - **Saved-place recommendation** ("where should I eat tonight?") → unchanged;
     `find_saved` + `suggest_places` as before.
4. Confirm `data.tool_results` carries a `research` payload only on research
   turns, and reasoning steps stream the single user-visible research row.
   Also confirm a research-only turn writes **no** `user_intents` row
   (`GET /v1/user/intents` unchanged after it) while a place turn still does.
5. **Harvest (§3b):** run the harvester over a fixture post carrying a practical
   fact (e.g. "cash only, ATM inside charges a fee") and confirm the emitted
   claim is tagged from the controlled vocabulary and readable by research —
   closing the write→read loop end-to-end.

---

## Notable constraints honored

- **ADR-120:** claims read by exact entity key only; topic ranking is in-memory
  over an already-entity-scoped set, not store-level semantic search.
- **ADR-122:** `approved_only=True` on every research read.
- **ADR-124/126:** entity resolution is verified-or-refuse; ambiguity →
  clarify; a named entity is never keyed to a nearby/stale one.
- **ADR-105:** the tool returns an explicit DTO (`ResearchResult`), not a
  persistence model.
- Provider-agnostic reuse of the free Nominatim `EntityGeoResolver`; no new
  external dep (per `feedback_prefer_free_geocoding`).
