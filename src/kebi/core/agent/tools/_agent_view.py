"""Lean projections of tool results for the orchestrator (ADR-139).

A `ConsultResult` is the *server's* shape: it carries catalog ids, provider
ids, retrieval scores, coordinates, and timestamps because the linker, the
signal path, and the client all need them. The orchestrator needs none of it.
Sending the whole thing cost roughly 390 tokens per candidate — so a
`find_saved(limit=10)` alone ran about 4,000 tokens, re-read on every loop
tick — and a third of that budget was spent on fields the prompt explicitly
forbids the model from using (`rrf_score`, `vector_rank`, `text_rank`).

So the tool message carries only what writing the answer requires: what the
place is called, where it is, what kind of place it is, how the user knows it,
and what kebi knows about it. Everything else stays server-side on the
`tool_payloads` channel, which is what the linker and the response layer read.

Cutting this is not only about cost. Every token of retrieval plumbing in the
context is a token competing with the claims for the model's attention, and
the claims are the part that makes an answer ours.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kebi.core.agent.tools.consult_models import ConsultCandidate, ConsultResult
    from kebi.core.knowledge.research_models import ResearchResult
    from kebi.core.web.models import WebSearchResult

# Enough tags to characterise a place ("rooftop, lively, open_late"), few
# enough that a 10-candidate list does not become a tag dump. Ordered as
# stored, so provider-attested values come first.
_MAX_TAGS = 6


def _tag_values(place: Any) -> list[str]:
    """Flat tag values — the `{type, value, source}` triple is server bookkeeping."""
    values: list[str] = []
    for tag in place.tags or []:
        raw = getattr(tag, "value", None)
        value = getattr(raw, "value", raw)
        if isinstance(value, str) and value not in values:
            values.append(value)
        if len(values) >= _MAX_TAGS:
            break
    return values


def _where(place: Any) -> str | None:
    """A human place-reference: the street, then the area it is in.

    The model reasons about "over in seminyak, not worth the ride" from names,
    not from lat/lng, and it cannot do arithmetic on coordinates anyway. A
    provider address is a full postal line ("Jl. Batu Mejan Canggu, Canggu,
    Kec. Kuta Utara, Kabupaten Badung, Bali 80351, Indonesia") whose tail is
    administrative noise, so only the street segment is kept, and the area is
    appended only when the street does not already name it.
    """
    loc = getattr(place, "location", None)
    if loc is None:
        return None
    parts: list[str] = []
    address = getattr(loc, "address", None)
    if isinstance(address, str) and address:
        parts.append(address.split(",")[0].strip())
    area = getattr(loc, "neighborhood", None) or getattr(loc, "city", None)
    if isinstance(area, str) and area:
        joined = " ".join(parts).casefold()
        if area.casefold() not in joined:
            parts.append(area)
    return ", ".join(p for p in parts if p) or None


def _saved_context(user_data: Any) -> dict[str, Any] | None:
    """How this user knows the place — provenance, verdict, their own note.

    `source` is what lets an answer say "that's the one from your video", so
    it stays; the join keys and timestamps do not.
    """
    if user_data is None:
        return None
    context: dict[str, Any] = {"saved_from": getattr(user_data, "source", None)}
    if getattr(context["saved_from"], "value", None):
        context["saved_from"] = context["saved_from"].value
    if getattr(user_data, "visited", False):
        context["visited"] = True
    liked = getattr(user_data, "liked", None)
    if liked is not None:
        context["liked"] = liked
    note = getattr(user_data, "note", None)
    if note:
        context["their_note"] = note
    return context


def candidate_view(candidate: ConsultCandidate) -> dict[str, Any]:
    """One candidate as the orchestrator needs to see it."""
    place = candidate.place
    view: dict[str, Any] = {
        "name": candidate.display_name,
        "source": candidate.source,
    }
    where = _where(place)
    if where:
        view["where"] = where
    if place.categories:
        view["categories"] = [getattr(c, "value", c) for c in place.categories]
    tags = _tag_values(place)
    if tags:
        view["tags"] = tags
    saved = _saved_context(candidate.user_data)
    if saved:
        view["saved"] = saved
    if candidate.reason:
        view["reason"] = candidate.reason
    if candidate.notes:
        # Claim text only. Ids, confidences, and vote tallies are ranking
        # inputs the service already applied — the order IS the ranking.
        view["kebi_knows"] = [n.text for n in candidate.notes]
    return view


def consult_view(result: ConsultResult) -> dict[str, Any]:
    """A whole place-tool result, minus everything the model cannot use."""
    view: dict[str, Any] = {
        "candidates": [candidate_view(c) for c in result.candidates]
    }
    if result.empty_reason:
        view["empty_reason"] = result.empty_reason
    if result.area_notes:
        view["kebi_knows_about_the_area"] = [n.text for n in result.area_notes]
    return view


def research_view(result: ResearchResult) -> dict[str, Any]:
    """A research result: the notes and, on empty, what to ask about."""
    view: dict[str, Any] = {}
    if result.entity_name:
        view["about"] = result.entity_name
    if result.notes:
        view["notes"] = [n.text for n in result.notes]
    if result.empty_reason:
        view["empty_reason"] = result.empty_reason
    if result.clarification:
        view["clarification"] = result.clarification
    return view


def web_search_view(result: WebSearchResult) -> dict[str, Any]:
    """A web search: the text, who published it, and how old it is.

    The URL is dropped on purpose. The model cannot follow a link, the chat
    contract has nowhere to render one (ADR-136), and a URL is 15-30 tokens of
    pure cost per finding. `source` (the bare domain) is what an answer
    actually uses — "the schedule on fifa.com still has it at 9" — and `age`
    is what lets it hedge honestly when the page is a year old.
    """
    view: dict[str, Any] = {}
    if result.findings:
        view["findings"] = [
            {
                k: v
                for k, v in (
                    ("text", f.text),
                    ("source", f.source),
                    ("published", f.age),
                )
                if v
            }
            for f in result.findings
        ]
    if result.empty_reason:
        view["empty_reason"] = result.empty_reason
    return view
