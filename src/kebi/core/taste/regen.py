"""Prompt builder, artifact validation, and agent formatting (ADR-077).

Grounding is shape-agnostic: `_resolve_path` walks any nested dict by
dotted `source_field`, so the places-vocabulary signal_counts works
unchanged (e.g. `categories`, `tags.price`, `location.city`,
`rejected.categories`).

- build_regen_messages: construct system + user messages for LLM call.
- validate_grounded: drop SummaryLine items not backed by signal_counts.
- format_summary_for_agent: join structured summary to bullet-point text.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kebi.core.config import get_prompt
from kebi.core.taste.aggregation import SignalCounts
from kebi.core.taste.schemas import SummaryLine, TasteArtifacts

logger = logging.getLogger(__name__)


def load_regen_prompt_template() -> str:
    """Load the taste_regen prompt via ADR-059 config-driven loader."""
    return get_prompt("taste_regen")


def build_regen_messages(
    signal_counts: SignalCounts,
    early_signal_threshold: int,
) -> list[dict[str, str]]:
    """Build system + user messages for taste artifact generation."""
    template = load_regen_prompt_template()
    system_prompt = template.replace(
        "{early_signal_threshold}", str(early_signal_threshold)
    )
    body: dict[str, Any] = signal_counts.model_dump(exclude_defaults=False)
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
    """Validate summary lines against signal_counts.

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

    if dropped:
        logger.warning("Dropped %d ungrounded items during validation", len(dropped))

    validated = TasteArtifacts(summary=valid_summary)
    return validated, dropped


def format_summary_for_agent(lines: list[SummaryLine]) -> str:
    """Join structured summary back to bullet-point text for agent prompt injection."""
    return "\n".join(f"- {line.text} [{line.signal_count} signals]" for line in lines)
