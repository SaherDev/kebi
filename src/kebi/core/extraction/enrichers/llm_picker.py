"""LLM place-picker — picks from the search-result set, classifies, infers (v2).

Runs after the pipeline's search step has populated a `search_set` of
attributed v2 `PlaceObject`s (one per `KnownPlace` + `location_tag`
query that produced a hit). The picker decides which of those hits
the post actually references and enriches each pick with v2
vocabulary: `categories` (`PlaceCategory`) and `tags` (`PlaceTag`
with typed value).

The picker MUST NOT invent venues. Its `provider_id` is reduced to
the closed set of keys in the prompt; `reconcile_picks` drops any
pick whose id escaped the set.

Empty `search_set` short-circuits before the LLM call.

Emits places vocabulary (categories + typed tags) — ADR-070.
"""

from __future__ import annotations

import logging
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from kebi.core.agent._trace_context import traced_call
from kebi.core.config import ConfidenceConfig, get_prompt
from kebi.core.extraction.candidate_mapper import (
    AttributedSearchResult,
    evidence_field_to_medium,
    llm_tags_to_place_tags,
    merge_tags,
    reconcile_picks,
)
from kebi.core.extraction.types import (
    Evidence,
    EvidenceField,
    ExtractionContext,
    Medium,
    PickOutcome,
    Producer,
    ValidatedCandidate,
)
from kebi.core.places import PlaceCategory, PlaceTag, normalize_icon
from kebi.providers.llm import InstructorClient

logger = logging.getLogger(__name__)


class _LLMTag(BaseModel):
    """Flat tag shape the LLM emits; converted to `PlaceTag` at the boundary.

    Kept str-typed so the LLM has flexibility while still respecting the
    TagType vocabulary on the way out. `source` is stamped as `"llm"`
    by the caller — the LLM never sets it.
    """

    type: str = Field(
        description=(
            "TagType axis. One of: cuisine, dietary, feature, atmosphere, "
            "service, price, time, season. Never accessibility."
        ),
    )
    value: str = Field(
        description=(
            "Tag value. Use canonical values where they exist "
            "(e.g. cuisine: Thai, Japanese, Italian; "
            "atmosphere: cozy, lively, romantic, scenic; "
            "service: dine_in, reservable, serves_dinner, serves_cocktails; "
            "feature: rooftop, outdoor_seating, live_music; "
            "price: budget, moderate, expensive, very_expensive)."
        ),
    )

    model_config = ConfigDict(extra="forbid")


class _PickedPlace(BaseModel):
    """LLM output schema — one entry per search candidate the post references.

    `provider_id` MUST equal one of the values in `<search_candidates>`;
    `reconcile_picks` drops picks that violate this.

    Set `rejected=true` (with `rejection_reason`) if the candidate
    looks wrong — rather than emitting a bad pick. Rejected picks are
    logged but not persisted.
    """

    provider_id: str = Field(
        min_length=1,
        description=("Must match one of the provider_id values in search_candidates."),
    )
    categories: list[PlaceCategory] = Field(
        default_factory=list,
        max_length=3,
        description=(
            "1-3 PlaceCategory values, most-specific first. Derive them from "
            "the venue's OWN identity — its name plus the post — and pick the "
            "most specific fit; never just echo a generic search label. "
            "Examples: a name ending in 'Beach' -> beach; a 'Wat'/temple/Buddha "
            "site -> temple (or shrine); a shopping mall or department store -> "
            "shopping_mall; a named 'Viewpoint' -> viewpoint; a 'Market' -> the "
            "matching *_market. Use `landmark` ONLY for a notable attraction "
            "with no more specific category. Leave empty only when nothing in "
            "the vocabulary fits (e.g. a whole town, island, or region)."
        ),
    )
    tags: list[_LLMTag] = Field(default_factory=list)
    icon: str | None = Field(
        default=None,
        description=(
            "One emoji capturing THIS venue's identity (🗼 iconic tower, "
            "⛲ fountain, 🌴 palm-defined place). Prefer specific over "
            "generic; omit (null) when nothing beats the generic category "
            "default — never emit 📍 or 📸."
        ),
    )
    subcategory: str | None = None
    evidence_fields: list[EvidenceField] = Field(default_factory=list)
    rejected: bool = False
    rejection_reason: str | None = None
    rejection_kind: Literal["non_venue", "other"] | None = Field(
        default=None,
        description=(
            "Set only when rejected=true. 'non_venue' means the referenced "
            "name is a route, road, pass, loop, trail, region, island, town, "
            "neighborhood, or natural feature — not a visitable venue. "
            "'other' (or null) for any other rejection (wrong place, "
            "unrelated result)."
        ),
    )

    model_config = ConfigDict(extra="forbid")


class _PickerResponse(BaseModel):
    picks: list[_PickedPlace]


class LLMPlacePicker:
    """LLM picker — runs after the pipeline's search step, before persistence.

    Reads `context` (caption/transcript/title/hashtags/location_tag/
    supplementary_text) and a `search_set` dict keyed by `provider_id`.
    Returns `ValidatedCandidate`s ready for the persistence boundary.

    Empty `search_set` → no LLM call, returns []. The pipeline's
    next-level fallback handles that case at the loop level.
    """

    def __init__(
        self,
        instructor_client: InstructorClient,
        confidence_config: ConfidenceConfig,
    ) -> None:
        self._instructor_client = instructor_client
        self._confidence_config = confidence_config

    async def pick(
        self,
        context: ExtractionContext,
        search_set: dict[str, AttributedSearchResult],
        shared_tags: list[PlaceTag] | None = None,
    ) -> PickOutcome:
        """Classify search results into picks (ADR-080: post-search half).

        `shared_tags` are post-level attribute tags from the resolver,
        merged into every pick (per-place tags win on conflict). Default
        `None` preserves pre-ADR-080 behavior when no resolver is wired.

        Besides the kept candidates, the outcome carries the display
        labels of picks the LLM rejected as non-venue geography, so the
        caller can narrate them as noted interests (never a silent drop).
        """
        if not search_set:
            return PickOutcome(candidates=[])

        user_content = self._build_prompt(context, search_set)
        # Phase 4.5 subtask 2: nests under the extraction_run trace.
        # Same shape as `LLMResolver` — Instructor doesn't expose token
        # counts, so usage is left for Langfuse to approximate from the
        # input/output payloads.
        async with traced_call(
            "extraction.llm_picker",
            "extraction",
            role="extractor",
            user_id=context.user_id,
            extra={
                "search_candidate_count": len(search_set),
                "caption_length": len(context.caption or ""),
                "transcript_length": len(context.transcript or ""),
            },
        ) as t:
            try:
                response = cast(
                    _PickerResponse,
                    await self._instructor_client.extract(
                        response_model=_PickerResponse,
                        messages=[
                            {
                                "role": "system",
                                "content": get_prompt("place_classifier"),
                            },
                            {"role": "user", "content": user_content},
                        ],
                    ),
                )
            except Exception as exc:
                t.fail(exc)
                logger.warning("LLMPlacePicker failed: %s", exc, exc_info=True)
                return PickOutcome(candidates=[])

            kept = [p for p in response.picks if not p.rejected]
            rejected = [p for p in response.picks if p.rejected]
            t.output = {
                "picked_count": len(kept),
                "rejected_count": len(rejected),
            }
            non_venue_names: list[str] = []
            for r in rejected:
                logger.info(
                    "place_picker_rejected",
                    extra={
                        "provider_id": r.provider_id,
                        "reason": r.rejection_reason,
                        "kind": r.rejection_kind,
                    },
                )
                # A non-venue rejection is narrated, not just logged. The
                # post's own label for the place (display_label, empty ⇒
                # the raw query name) is what the user recognizes — not
                # the surviving search candidate's name.
                if r.rejection_kind == "non_venue":
                    attributed = search_set.get(r.provider_id)
                    if attributed is not None:
                        non_venue_names.append(
                            attributed.display_label or attributed.query
                        )

            intermediate: list[ValidatedCandidate] = [
                c
                for c in (self._to_intermediate(p, search_set) for p in kept)
                if c is not None
            ]
            if shared_tags:
                for c in intermediate:
                    c.tags = merge_tags(c.tags, shared_tags)
            return PickOutcome(
                candidates=reconcile_picks(
                    picks=intermediate,
                    search_set=search_set,
                    confidence_config=self._confidence_config,
                    context=context,
                ),
                non_venue_names=non_venue_names,
            )

    @staticmethod
    def _to_intermediate(
        pick: _PickedPlace,
        search_set: dict[str, AttributedSearchResult],
    ) -> ValidatedCandidate | None:
        """Pack the LLM pick into a ValidatedCandidate intermediate.

        `place_name`, `provider_id`, `confidence`, `location` are
        placeholders — `reconcile_picks` overrides them with the
        authoritative v2 PlaceObject values. `categories`, `tags`, and
        `subcategory` are the picker's contribution and survive the
        reconcile pass.
        """
        if pick.provider_id not in search_set:
            return None

        # Sanitize subcategory — LLMs occasionally emit "null" / "" / "none"
        # instead of the JSON null.
        subcategory = pick.subcategory
        if subcategory in ("null", "none", ""):
            subcategory = None

        evidence = [
            Evidence(
                producer=Producer.LLM_NER,
                medium=evidence_field_to_medium(field),
            )
            for field in pick.evidence_fields
        ]
        # Safety net: calculate_confidence requires non-empty evidence.
        if not evidence:
            evidence.append(Evidence(producer=Producer.LLM_NER, medium=Medium.CAPTION))

        # Convert flat LLM tags → PlaceTag with source="llm" via the
        # shared helper (also used by the resolver for post-level tags).
        tags = llm_tags_to_place_tags(pick.tags)

        return ValidatedCandidate(
            place_name="",  # overridden in reconcile
            provider_id=pick.provider_id,
            categories=list(pick.categories),
            tags=tags,
            icon=normalize_icon(pick.icon),
            confidence=0.0,  # overridden in reconcile
            evidence=evidence,
            subcategory=subcategory,
            location=None,  # overridden in reconcile
        )

    @staticmethod
    def _build_prompt(
        context: ExtractionContext,
        search_set: dict[str, AttributedSearchResult],
    ) -> str:
        platform = context.platform or "unknown"
        title = context.title or ""
        caption = context.caption or context.supplementary_text or ""
        transcript = context.transcript or ""
        hashtags = context.hashtags or []
        location_tag = context.location_tag or ""
        supplementary = (
            context.supplementary_text
            if context.supplementary_text and context.supplementary_text != caption
            else ""
        )

        text_block = (
            "<text>\n"
            f"  platform:           {platform}\n"
            f"  title:              {title}\n"
            f"  caption:            {caption}\n"
            f"  transcript:         {transcript}\n"
            f"  hashtags:           {hashtags}\n"
            f"  location_tag:       {location_tag}\n"
            f"  supplementary_text: {supplementary}\n"
            "</text>\n"
        )

        # The venue names the post actually references, as extracted by
        # the producers (vision OCR, Google Maps list, caption NER).
        # Without this the classifier only sees hashtags + a flat
        # candidate list and cannot tell which candidates the post is
        # about — it would conservatively keep only the most famous one.
        seen_names: set[str] = set()
        referenced: list[str] = []
        for kp in context.known_places:
            n = (kp.name or "").strip()
            if n and n.lower() not in seen_names:
                seen_names.add(n.lower())
                referenced.append(n)
        post_places_block = (
            "<post_places>\n"
            + "\n".join(f"  - {n}" for n in referenced)
            + "\n</post_places>\n"
            if referenced
            else ""
        )

        match_lines = ["<search_candidates>"]
        for ar in search_set.values():
            place = ar.place
            cats = ", ".join(c.value for c in place.categories) or "(none)"
            loc = place.location
            loc_str = ""
            if loc is not None:
                bits = [loc.neighborhood, loc.city, loc.country, loc.address]
                loc_str = " | ".join(b for b in bits if b)
            match_lines.append(
                f"  - provider_id: {place.provider_id}\n"
                f"    name:        {place.place_name}\n"
                f"    location:    {loc_str}\n"
                f"    categories:  {cats}\n"
                f"    matched_via: {ar.query_producer.value}/{ar.query_medium.value}"
                f' (query "{ar.query}")'
            )
        match_lines.append("</search_candidates>\n")
        match_block = "\n".join(match_lines)

        return (
            text_block
            + "\n"
            + post_places_block
            + "\n"
            + match_block
            + "\n"
            + "The post references the venues in <post_places>. For EACH "
            "referenced venue, pick the matching entry from "
            "<search_candidates> (use <text> location signals to "
            "disambiguate same-name candidates). Emit one structured "
            "object per matched venue. Only `rejected=true` a candidate "
            "that is genuinely wrong (no real match for a referenced "
            "venue, or wrong city) — do not drop a clearly-referenced "
            "venue silently.\n\n" + _VOCAB_INSTRUCTION
        )


_VOCAB_INSTRUCTION = """\
PlaceCategory values (use 1-3 most-specific values):
  food_and_drink:
    restaurant, cafe, bar, pub, bakery, dessert_shop, ice_cream_shop,
    street_food, food_court, food_market, brewery, winery, distillery,
    tea_house, juice_bar
  retail:
    grocery_store, supermarket, convenience_store, shopping_mall, boutique,
    bookstore, specialty_shop, farmers_market, flea_market, night_market,
    pharmacy, electronics_store
  culture / sightseeing:
    museum, art_gallery, historical_site, monument, temple, church, mosque,
    shrine, landmark, viewpoint
  entertainment:
    theme_park, amusement_park, zoo, aquarium, botanical_garden, cinema,
    theater, concert_hall, live_music_venue, nightclub, comedy_club,
    karaoke, arcade, bowling_alley, billiards_hall
  nature / outdoors:
    park, beach, hiking_trail, lake, river, garden, campground, scenic_lookout
  fitness / wellness:
    gym, fitness_studio, yoga_studio, pilates_studio, spa, massage,
    hot_spring, bathhouse, salon, barber
  services / utilities:
    atm, bank, post_office, gas_station, parking, laundry
  accommodation:
    hotel, hostel, guesthouse, bed_and_breakfast, resort, vacation_rental
  transit:
    airport, train_station, metro_station, bus_terminal, ferry_terminal
  sport / recreation:
    stadium, arena, sports_club, swimming_pool, climbing_gym, skate_park,
    golf_course
  work / study:
    coworking_space, library, study_cafe

TagType axes (use for `type` field on each tag):
  - cuisine     — Thai, Japanese, Korean, Italian, Mexican, Vietnamese,
                  Mediterranean, French, Indian, Chinese, American, etc.
  - dietary     — vegan, vegetarian, halal, vegetarian_options
  - feature     — outdoor_seating, indoor, rooftop, garden, scenic_view,
                  dog_friendly, family_friendly, parking, live_music, etc.
  - atmosphere  — cozy, romantic, trendy, quiet, lively, intimate,
                  upscale, hidden_gem, casual, modern, traditional, etc.
  - service     — dine_in, takeout, delivery, reservable,
                  serves_breakfast, serves_brunch, serves_lunch,
                  serves_dinner, serves_beer, serves_wine, serves_cocktails
  - price       — free, budget, moderate, expensive, very_expensive
  - time        — morning, brunch, lunch, afternoon, evening, night,
                  late_night, all_day
  - season      — summer, winter, rainy, spring, autumn, all_season

`evidence_fields` lists which text sources you actually used to support
this pick — pick from: caption, transcript, title, hashtag,
location_tag, supplementary_text, known_places.

REMEMBER: these tags are the product's only source of experiential data
— tag generously but truthfully, from two evidence sources:
  1. Post content — what the caption/transcript/title shows or says
     (rooftop shots -> rooftop; "so cheap" -> budget; cocktail menus ->
     serves_cocktails; dinner-service framing -> serves_dinner).
  2. World knowledge of the identified venue — the candidate is already
     validated with a canonical name + location; if you confidently know
     THIS specific venue (a famous steakhouse -> steakhouse categories +
     expensive + serves_dinner + reservable), tag from knowledge even
     when the post never says it. Skip world-knowledge tags for venues
     you don't specifically recognize.
Price may be inferred from content signals or obvious venue identity.
NEVER emit accessibility tags (wheelchair_*) — not from the post, not
from world knowledge. Unverified accessibility claims cause real-world
harm; they are dropped in code regardless.\
"""
