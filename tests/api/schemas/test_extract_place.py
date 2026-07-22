"""Tests for the v2 ExtractPlaceResponse / ExtractPlaceItem envelope.

Spec 030 Phase 6: ExtractPlaceItem no longer carries a per-item
`status` field (ADR-071 — every picker output saves; the
saved/duplicate distinction is internal). The envelope-level status
literal stays `{pending, completed, failed}` per ADR-063.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kebi.api.schemas.extract_place import (
    ExtractPlaceItem,
    ExtractPlaceResponse,
    NotedInterest,
)
from kebi.core.places import PlaceCategory, PlaceObject


def _make_place(name: str = "Nara Eatery", place_id: str = "pl_01HZ001") -> PlaceObject:
    return PlaceObject(
        id=place_id,
        provider_id="google:" + place_id,
        place_name=name,
        categories=[PlaceCategory.restaurant],
    )


class TestExtractPlaceItem:
    def test_requires_non_null_place(self) -> None:
        with pytest.raises(ValidationError):
            ExtractPlaceItem(place=None, confidence=0.9)  # type: ignore[arg-type]

    def test_requires_non_null_confidence(self) -> None:
        with pytest.raises(ValidationError):
            ExtractPlaceItem(place=_make_place(), confidence=None)  # type: ignore[arg-type]

    def test_no_status_field(self) -> None:
        """status was removed in spec 030 Phase 6 (ADR-071)."""
        assert "status" not in ExtractPlaceItem.model_fields

    def test_minimal_item_ok(self) -> None:
        item = ExtractPlaceItem(place=_make_place(), confidence=0.8)
        assert item.place.place_name == "Nara Eatery"
        assert item.confidence == 0.8

    @pytest.mark.parametrize("confidence", [-0.1, 1.1, 2.0])
    def test_confidence_out_of_range(self, confidence: float) -> None:
        with pytest.raises(ValidationError):
            ExtractPlaceItem(place=_make_place(), confidence=confidence)

    def test_confidence_boundaries_accepted(self) -> None:
        ExtractPlaceItem(place=_make_place(), confidence=0.0)
        ExtractPlaceItem(place=_make_place(), confidence=1.0)


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
        item = ExtractPlaceItem(place=_make_place(), confidence=0.9)
        with pytest.raises(ValidationError):
            ExtractPlaceResponse(
                status="completed",
                results=[item],
                failure_reason="no_candidates",
            )

    def test_completed_with_results_ok(self) -> None:
        item = ExtractPlaceItem(place=_make_place(), confidence=0.87)
        resp = ExtractPlaceResponse(
            status="completed",
            results=[item],
            raw_input="https://tiktok.com/@x/video/123",
            request_id="req_03",
        )
        assert resp.status == "completed"
        assert len(resp.results) == 1

    def test_completed_requires_non_empty_results(self) -> None:
        with pytest.raises(ValidationError):
            ExtractPlaceResponse(status="completed", results=[], raw_input="...")

    def test_pending_forbids_non_empty_results(self) -> None:
        item = ExtractPlaceItem(place=_make_place(), confidence=0.8)
        with pytest.raises(ValidationError):
            ExtractPlaceResponse(status="pending", results=[item], raw_input="...")

    def test_failed_forbids_non_empty_results(self) -> None:
        item = ExtractPlaceItem(place=_make_place(), confidence=0.8)
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


class TestNotedInterests:
    """Location-kinds Step 1: completed may carry acknowledgments for
    detected non-venue geography, including with empty results."""

    @staticmethod
    def _noted(name: str = "Ha Giang Loop") -> NotedInterest:
        return NotedInterest(name=name, message=f"'{name}' noted as an interest.")

    def test_completed_with_only_noted_interests_ok(self) -> None:
        resp = ExtractPlaceResponse(
            status="completed",
            results=[],
            raw_input="https://tiktok.com/@x/video/123",
            request_id="req_04",
            noted_interests=[self._noted()],
        )
        assert resp.results == []
        assert resp.noted_interests[0].name == "Ha Giang Loop"

    def test_completed_with_results_and_noted_interests_ok(self) -> None:
        item = ExtractPlaceItem(place=_make_place(), confidence=0.9)
        resp = ExtractPlaceResponse(
            status="completed",
            results=[item],
            noted_interests=[self._noted("Hai Van Pass")],
        )
        assert len(resp.results) == 1
        assert len(resp.noted_interests) == 1

    def test_completed_with_neither_fails(self) -> None:
        with pytest.raises(ValidationError):
            ExtractPlaceResponse(status="completed", results=[], noted_interests=[])

    def test_failed_forbids_noted_interests(self) -> None:
        with pytest.raises(ValidationError):
            ExtractPlaceResponse(
                status="failed",
                results=[],
                failure_reason="no_candidates",
                noted_interests=[self._noted()],
            )

    def test_pending_forbids_noted_interests(self) -> None:
        with pytest.raises(ValidationError):
            ExtractPlaceResponse(
                status="pending",
                results=[],
                noted_interests=[self._noted()],
            )
