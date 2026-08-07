"""`how_you_know` — attested connection lines the answer must voice.

Prose attribution proved model-flaky in live runs, so the connections are
derived here deterministically and handed to the model ready to phrase. A
line only ever states what the candidate's own data attests.
"""

from __future__ import annotations

from datetime import UTC, datetime

from kebi.core.agent.tools._agent_view import candidate_view
from kebi.core.agent.tools.consult_models import ConsultCandidate
from kebi.core.places.models import PlaceCore, PlaceSource, PlaceTag, UserPlace


def _place(tag_value: str | None = None) -> PlaceCore:
    tags = (
        [PlaceTag(type="atmosphere", value=tag_value, source="llm")]
        if tag_value
        else []
    )
    return PlaceCore(id="p1", place_name="Kala Kala", tags=tags)


def _saved(source: PlaceSource) -> UserPlace:
    return UserPlace(
        user_place_id="up1",
        user_id="u1",
        place_id="p1",
        source=source,
        # A share-sourced save attests the URL it came from.
        source_ref=(
            "https://tiktok.com/@u/video/1"
            if source == PlaceSource.tiktok
            else None
        ),
        saved_at=datetime.now(UTC),
    )


def _candidate(**kw: object) -> ConsultCandidate:
    defaults: dict[str, object] = {
        "place": _place(),
        "user_data": None,
        "source": "suggested",
        "rrf_score": 0.0,
    }
    defaults.update(kw)
    return ConsultCandidate(**defaults)  # type: ignore[arg-type]


def test_a_share_saved_place_says_the_share() -> None:
    view = candidate_view(
        _candidate(user_data=_saved(PlaceSource.tiktok), source="saved")
    )
    assert view["how_you_know"] == ["in their library, saved from a tiktok they shared"]


def test_a_manual_save_is_theirs_directly() -> None:
    view = candidate_view(
        _candidate(user_data=_saved(PlaceSource.manual), source="saved")
    )
    assert view["how_you_know"] == ["in their library, saved by them directly"]


def test_a_taste_match_names_the_taste() -> None:
    view = candidate_view(
        _candidate(place=_place("beach_club")),
        taste_values=["beach_club", "photo_spot"],
    )
    assert view["how_you_know"] == ["matches their taste, they keep saving beach_club"]


def test_saved_and_taste_stack() -> None:
    view = candidate_view(
        _candidate(
            place=_place("beach_club"),
            user_data=_saved(PlaceSource.tiktok),
            source="saved",
        ),
        taste_values=["beach_club"],
    )
    assert view["how_you_know"] == [
        "in their library, saved from a tiktok they shared",
        "matches their taste, they keep saving beach_club",
    ]


def test_no_connection_means_no_key_not_an_empty_claim() -> None:
    view = candidate_view(_candidate(), taste_values=["beach_club"])
    assert "how_you_know" not in view
