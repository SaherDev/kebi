"""Tests for the consult tool output shape (ConsultResult).

The contract that matters here: every consult result auto-mints a
`recommendation_id` so the client can attribute a later accept/reject/save
signal back to it, and that id survives the JSON round-trip the tool layer
does on the way to the agent / client.
"""

from __future__ import annotations

from kebi.core.agent.tools.consult_models import ConsultResult


def test_recommendation_id_is_auto_minted() -> None:
    result = ConsultResult()

    assert result.recommendation_id
    # A uuid4 string — 36 chars with dashes.
    assert len(result.recommendation_id) == 36


def test_each_result_gets_a_distinct_id() -> None:
    a = ConsultResult()
    b = ConsultResult()

    assert a.recommendation_id != b.recommendation_id


def test_recommendation_id_survives_json_round_trip() -> None:
    result = ConsultResult(empty_reason="no_match")

    reloaded = ConsultResult.model_validate_json(result.model_dump_json())

    assert reloaded.recommendation_id == result.recommendation_id


def test_explicit_recommendation_id_is_preserved() -> None:
    """A caller (or a replayed payload) can pin the id rather than minting."""
    result = ConsultResult(recommendation_id="rec-fixed-1")

    assert result.recommendation_id == "rec-fixed-1"
