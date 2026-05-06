"""Tests for calculate_confidence() on the evidence trail."""

import pytest

from totoro_ai.core.config import ConfidenceConfig
from totoro_ai.core.extraction.confidence import calculate_confidence
from totoro_ai.core.extraction.types import Evidence, Medium, Producer

_config = ConfidenceConfig(
    producer_scores={
        "llm_ner": 0.80,
        "subtitle_check": 0.75,
        "whisper_audio": 0.65,
        "vision_frames": 0.55,
        "google_maps_list": 0.95,
    },
    medium_scores={
        "caption": 0.75,
        "transcript": 0.65,
        "frame": 0.55,
        "list": 0.95,
        "emoji_marker": 0.92,
    },
    corroboration_bonus=0.10,
    max_score=0.97,
)


class TestCalculateConfidence:
    def test_single_evidence_uses_max_of_producer_or_medium(self) -> None:
        # producer=0.80, medium=0.75 → max=0.80; *1.0 + 0 = 0.80
        evidence = [Evidence(Producer.LLM_NER, Medium.CAPTION)]
        assert calculate_confidence(evidence, 1.0, _config) == pytest.approx(0.80)

    def test_single_evidence_no_corroboration_bonus(self) -> None:
        # Single evidence → no bonus.
        evidence = [Evidence(Producer.VISION_FRAMES, Medium.FRAME)]
        # max(0.55, 0.55) = 0.55; *1.0 = 0.55
        assert calculate_confidence(evidence, 1.0, _config) == pytest.approx(0.55)

    def test_two_distinct_pairs_apply_bonus(self) -> None:
        evidence = [
            Evidence(Producer.LLM_NER, Medium.CAPTION),
            Evidence(Producer.VISION_FRAMES, Medium.FRAME),
        ]
        # base = max(0.80, 0.55) = 0.80; *1.0 + 0.10 = 0.90.
        assert calculate_confidence(evidence, 1.0, _config) == pytest.approx(0.90)

    def test_corroboration_capped_at_max_score(self) -> None:
        evidence = [
            Evidence(Producer.GOOGLE_MAPS_LIST, Medium.LIST),
            Evidence(Producer.LLM_NER, Medium.CAPTION),
        ]
        # base = 0.95; *1.0 + 0.10 = 1.05 → capped at 0.97
        assert calculate_confidence(evidence, 1.0, _config) == pytest.approx(0.97)

    def test_match_modifier_scales_base_only(self) -> None:
        # FUZZY match modifier 0.9.
        evidence = [Evidence(Producer.LLM_NER, Medium.CAPTION)]
        # 0.80 * 0.9 = 0.72.
        assert calculate_confidence(evidence, 0.9, _config) == pytest.approx(0.72)

    def test_none_match_modifier_keeps_low_baseline(self) -> None:
        evidence = [Evidence(Producer.LLM_NER, Medium.CAPTION)]
        # 0.80 * 0.3 = 0.24.
        assert calculate_confidence(evidence, 0.3, _config) == pytest.approx(0.24)

    def test_emoji_marker_is_strongest_medium(self) -> None:
        evidence = [Evidence(Producer.LLM_NER, Medium.EMOJI_MARKER)]
        # max(producer=0.80, medium=0.92) = 0.92; *1.0 = 0.92.
        assert calculate_confidence(evidence, 1.0, _config) == pytest.approx(0.92)

    def test_same_pair_repeated_no_bonus(self) -> None:
        same = Evidence(Producer.LLM_NER, Medium.CAPTION)
        evidence = [same, same]
        # Only one distinct (producer, medium), no bonus.
        assert calculate_confidence(evidence, 1.0, _config) == pytest.approx(0.80)

    def test_unknown_keys_default_to_0_50(self) -> None:
        sparse = ConfidenceConfig(
            producer_scores={},
            medium_scores={},
            corroboration_bonus=0.10,
            max_score=0.97,
        )
        evidence = [Evidence(Producer.LLM_NER, Medium.CAPTION)]
        assert calculate_confidence(evidence, 1.0, sparse) == pytest.approx(0.50)

    def test_empty_evidence_returns_zero(self) -> None:
        # Defensive: should never happen by invariant, but degrades gracefully.
        assert calculate_confidence([], 1.0, _config) == 0.0
