"""Tests for the v2 ExtractPlaceResponse / ExtractPlaceItem envelope (ADR-063).

Covers:
- envelope status enum values
- results empty iff status != "completed"
- raw_input present on the envelope
- per-place status restricted to {saved, needs_review, duplicate}
- place and confidence required non-null on every item
- model_validator rejects status/results mismatches
- confidence range validator
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from totoro_ai.api.schemas.extract_place import (
    ExtractPlaceItem,
    ExtractPlaceResponse,
)
from totoro_ai.core.places import PlaceObject, PlaceType


def _make_place(name: str = "Nara Eatery", place_id: str = "pl_01HZ001") -> PlaceObject:
    return PlaceObject(
        place_id=place_id,
        place_name=name,
        place_type=PlaceType.food_and_drink,
        subcategory="restaurant",
    )


class TestExtractPlaceItem:
    def test_requires_non_null_place(self) -> None:
        with pytest.raises(ValidationError):
            ExtractPlaceItem(place=None, confidence=0.9, status="saved")  # type: ignore[arg-type]

    def test_requires_non_null_confidence(self) -> None:
        with pytest.raises(ValidationError):
            ExtractPlaceItem(place=_make_place(), confidence=None, status="saved")  # type: ignore[arg-type]

    def test_status_forbids_pending(self) -> None:
        with pytest.raises(ValidationError):
            ExtractPlaceItem(place=_make_place(), confidence=0.9, status="pending")  # type: ignore[arg-type]

    def test_status_forbids_failed(self) -> None:
        with pytest.raises(ValidationError):
            ExtractPlaceItem(place=_make_place(), confidence=0.9, status="failed")  # type: ignore[arg-type]

    @pytest.mark.parametrize("status", ["saved", "needs_review", "duplicate"])
    def test_valid_statuses(self, status: str) -> None:
        item = ExtractPlaceItem(place=_make_place(), confidence=0.8, status=status)  # type: ignore[arg-type]
        assert item.status == status

    @pytest.mark.parametrize("confidence", [-0.1, 1.1, 2.0])
    def test_confidence_out_of_range(self, confidence: float) -> None:
        with pytest.raises(ValidationError):
            ExtractPlaceItem(place=_make_place(), confidence=confidence, status="saved")

    def test_confidence_boundaries_accepted(self) -> None:
        ExtractPlaceItem(place=_make_place(), confidence=0.0, status="saved")
        ExtractPlaceItem(place=_make_place(), confidence=1.0, status="saved")


class TestExtractPlaceResponse:
    def test_pending_envelope_empty_results(self) -> None:
        resp = ExtractPlaceResponse(
            status="pending",
            results=[],
            raw_input="https://tiktok.com/@x/video/123",
            request_id="req_01",
        )
        assert resp.status == "pending"
        assert resp.results == []
        assert resp.raw_input == "https://tiktok.com/@x/video/123"
        assert resp.request_id == "req_01"

    def test_failed_envelope_empty_results(self) -> None:
        resp = ExtractPlaceResponse(
            status="failed",
            results=[],
            raw_input="plain text with no url",
            request_id="req_02",
            failure_reason="no_candidates",
            failure_message="No venue could be extracted.",
        )
        assert resp.status == "failed"
        assert resp.results == []
        assert resp.failure_reason == "no_candidates"

    def test_failed_requires_failure_reason(self) -> None:
        with pytest.raises(ValidationError):
            ExtractPlaceResponse(status="failed", results=[])

    def test_completed_forbids_failure_reason(self) -> None:
        item = ExtractPlaceItem(place=_make_place(), confidence=0.9, status="saved")
        with pytest.raises(ValidationError):
            ExtractPlaceResponse(
                status="completed",
                results=[item],
                failure_reason="no_candidates",
            )

    def test_completed_with_results_ok(self) -> None:
        item = ExtractPlaceItem(place=_make_place(), confidence=0.87, status="saved")
        resp = ExtractPlaceResponse(
            status="completed",
            results=[item],
            raw_input="https://tiktok.com/@x/video/123",
            request_id="req_03",
        )
        assert resp.status == "completed"
        assert len(resp.results) == 1

    def test_mixed_outcome_completed(self) -> None:
        saved = ExtractPlaceItem(
            place=_make_place(name="A", place_id="pl_A"),
            confidence=0.9,
            status="saved",
        )
        duplicate = ExtractPlaceItem(
            place=_make_place(name="B", place_id="pl_B"),
            confidence=0.95,
            status="duplicate",
        )
        resp = ExtractPlaceResponse(
            status="completed",
            results=[saved, duplicate],
            raw_input="...",
        )
        assert {r.status for r in resp.results} == {"saved", "duplicate"}

    def test_completed_requires_non_empty_results(self) -> None:
        with pytest.raises(ValidationError):
            ExtractPlaceResponse(status="completed", results=[], raw_input="...")

    def test_pending_forbids_non_empty_results(self) -> None:
        item = ExtractPlaceItem(place=_make_place(), confidence=0.8, status="saved")
        with pytest.raises(ValidationError):
            ExtractPlaceResponse(status="pending", results=[item], raw_input="...")

    def test_failed_forbids_non_empty_results(self) -> None:
        item = ExtractPlaceItem(place=_make_place(), confidence=0.8, status="saved")
        with pytest.raises(ValidationError):
            ExtractPlaceResponse(
                status="failed",
                results=[item],
                raw_input="...",
                failure_reason="no_candidates",
            )

    def test_raw_input_is_verbatim(self) -> None:
        """raw_input is a pure echo — no trimming, no URL canonicalization."""
        gnarly = "  https://tiktok.com/@x/video/123?utm_src=SPAM&x=1   "
        resp = ExtractPlaceResponse(
            status="pending", results=[], raw_input=gnarly, request_id="r"
        )
        assert resp.raw_input == gnarly

    def test_raw_input_optional(self) -> None:
        resp = ExtractPlaceResponse(
            status="failed",
            results=[],
            failure_reason="no_candidates",
        )
        assert resp.raw_input is None

    def test_no_source_url_field(self) -> None:
        """source_url was renamed to raw_input (ADR-063)."""
        assert "source_url" not in ExtractPlaceResponse.model_fields
        assert "raw_input" in ExtractPlaceResponse.model_fields
