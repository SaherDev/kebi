"""Extraction confidence scoring on the evidence trail (ADR-029).

Confidence is computed from the candidate's `evidence` list — every
producer/medium pair that contributed. Each evidence item has two
scores: one from `producer_scores` (which enricher contributed) and
one from `medium_scores` (where in pipeline state the evidence lived).
The candidate's base score is the strongest single piece — the max
across both axes for all evidence items. The corroboration bonus is
applied when the candidate has more than one distinct (producer,
medium) pair, which captures both "two enrichers agreed" and "one
enricher saw the name in two different media".
"""

from totoro_ai.core.config import ConfidenceConfig
from totoro_ai.core.extraction.types import Evidence


def calculate_confidence(
    evidence: list[Evidence],
    match_modifier: float,
    config: ConfidenceConfig,
) -> float:
    """Compute confidence using the evidence trail (ADR-029).

    Formula: min((base * match_modifier) + bonus, config.max_score)

    `base` is the strongest single piece of evidence:
        max(producer_scores[e.producer.value],
            medium_scores[e.medium.value]) for each e in evidence.

    `bonus` is `config.corroboration_bonus` when the candidate has
    more than one distinct (producer, medium) pair, otherwise 0.

    Args:
        evidence: List of `Evidence` items backing this candidate. By
            invariant non-empty (always at least one `LLM_NER` entry).
        match_modifier: Google Places match quality as float
            (1.0=exact, 0.3=none).
        config: ConfidenceConfig loaded from app.yaml.

    Returns:
        Confidence score in range [0.0, config.max_score].
    """
    if not evidence:
        return 0.0

    bases: list[float] = []
    for e in evidence:
        producer_score = config.producer_scores.get(e.producer.value, 0.50)
        medium_score = config.medium_scores.get(e.medium.value, 0.50)
        bases.append(max(producer_score, medium_score))
    base = max(bases)

    distinct_pairs = len({(e.producer, e.medium) for e in evidence})
    bonus = config.corroboration_bonus if distinct_pairs >= 2 else 0.0

    return min((base * match_modifier) + bonus, config.max_score)
