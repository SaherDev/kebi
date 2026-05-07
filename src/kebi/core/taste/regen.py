"""Prompt builder, artifact validation, and agent formatting (ADR-058).

- build_regen_messages: construct system + user messages for LLM call.
- validate_grounded: drop SummaryLine/Chip items not backed by signal_counts.
- format_summary_for_agent: join structured summary to bullet-point text.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kebi.core.config import get_prompt
from kebi.core.taste.aggregation import SignalCounts
from kebi.core.taste.schemas import Chip, SummaryLine, TasteArtifacts

logger = logging.getLogger(__name__)


def load_regen_prompt_template() -> str:
    """Load the taste_regen prompt via ADR-059 config-driven loader."""
    return get_prompt("taste_regen")


def _prune_path(body: dict[str, Any], source_field: str, source_value: str) -> None:
    """Remove `source_value` from the nested leaf dict at `source_field`.

    Walks the dotted path (e.g. "attributes.location_context.city") and
    deletes the key at the final dict. No-op if any segment is missing or
    the leaf isn't a dict. Used by `build_regen_messages` to scrub
    confirmed/rejected chip entries out of the behavioral-counts tree so
    the LLM doesn't see contradictory signals.
    """
    parts = source_field.split(".")
    current: Any = body
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return
        current = current[part]
    if isinstance(current, dict) and source_value in current:
        del current[source_value]


def build_regen_messages(
    signal_counts: SignalCounts,
    early_signal_threshold: int,
    existing_chips: list[Chip] | None = None,
) -> list[dict[str, str]]:
    """Build system + user messages for taste artifact generation (feature 023).

    If `existing_chips` contains any confirmed or rejected chips, they are
    serialized into the user-message JSON as `confirmed_chips` and
    `rejected_chips` arrays so the prompt can apply assertive/negative
    language and annotation rules. Those same entries are simultaneously
    pruned from the behavioral `signal_counts` tree so the LLM does not
    see the same (source_field, source_value) pair as both a positive
    behavioral signal AND an explicitly-decided preference — the
    contradiction was causing the LLM to fall back to behavioral wording
    even for rejected chips. Pending chips stay in `signal_counts`
    unchanged (they haven't been decided yet, so the behavioral tree is
    their only representation). When no chips are decided, both output
    arrays are omitted and the body is byte-identical to the pre-feature
    shape.
    """
    template = load_regen_prompt_template()
    system_prompt = template.replace(
        "{early_signal_threshold}", str(early_signal_threshold)
    )
    body: dict[str, Any] = signal_counts.model_dump(exclude_defaults=False)
    if existing_chips:
        confirmed = [
            {"source_field": c.source_field, "source_value": c.source_value}
            for c in existing_chips
            if c.status.value == "confirmed"
        ]
        rejected = [
            {"source_field": c.source_field, "source_value": c.source_value}
            for c in existing_chips
            if c.status.value == "rejected"
        ]
        # Prune decided chips from the behavioral counts so the LLM sees
        # no overlap between signal_counts and the confirmed/rejected arrays.
        for item in confirmed + rejected:
            _prune_path(body, item["source_field"], item["source_value"])
        if confirmed:
            body["confirmed_chips"] = confirmed
        if rejected:
            body["rejected_chips"] = rejected
    user_message = json.dumps(body, ensure_ascii=False)
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]


def _resolve_path(data: dict[str, Any], path: str) -> Any:
    """Walk a dotted path into a nested dict. Returns None if not found."""
    parts = path.split(".")
    current: Any = data
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _is_grounded(
    source_field: str,
    source_value: str | None,
    signal_counts_dict: dict[str, Any],
) -> bool:
    """Check if a source_field/source_value pair exists in signal_counts."""
    resolved = _resolve_path(signal_counts_dict, source_field)
    if resolved is None:
        return False
    if source_value is None:
        return True
    if isinstance(resolved, dict):
        return source_value in resolved
    return False


def validate_grounded(
    artifacts: TasteArtifacts,
    signal_counts: SignalCounts,
) -> tuple[TasteArtifacts, list[dict[str, Any]]]:
    """Validate both summary lines and chips against signal_counts.

    Returns a tuple of (validated artifacts, list of dropped items for logging).
    """
    sc_dict = signal_counts.model_dump(exclude_defaults=False)
    dropped: list[dict[str, Any]] = []

    valid_summary: list[SummaryLine] = []
    for line in artifacts.summary:
        if _is_grounded(line.source_field, line.source_value, sc_dict):
            valid_summary.append(line)
        else:
            dropped.append(
                {
                    "type": "summary",
                    "text": line.text,
                    "source_field": line.source_field,
                }
            )

    valid_chips: list[Chip] = []
    for chip in artifacts.chips:
        if chip.signal_count < 3:
            dropped.append(
                {"type": "chip", "label": chip.label, "reason": "signal_count < 3"}
            )
        elif _is_grounded(chip.source_field, chip.source_value, sc_dict):
            valid_chips.append(chip)
        else:
            dropped.append(
                {"type": "chip", "label": chip.label, "source_field": chip.source_field}
            )

    if dropped:
        logger.warning("Dropped %d ungrounded items during validation", len(dropped))

    validated = TasteArtifacts(summary=valid_summary, chips=valid_chips)
    return validated, dropped


def format_summary_for_agent(lines: list[SummaryLine]) -> str:
    """Join structured summary back to bullet-point text for agent prompt injection."""
    return "\n".join(f"- {line.text} [{line.signal_count} signals]" for line in lines)
