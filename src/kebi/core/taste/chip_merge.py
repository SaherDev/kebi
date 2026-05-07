"""Chip-status merge helpers (feature 023).

Two pure functions, separated by caller:

- `merge_chip_statuses(existing, submissions)` — applied when a chip_confirm
  signal arrives. Overwrites the `status` + `selection_round` of stored chips
  matched by `(source_field, source_value)`. Already-confirmed chips are
  preserved verbatim regardless of what the submission says (FR-006a).
  Chips in the submission that do not match any stored chip are ignored.
  Chips in `existing` not mentioned in the submission are preserved.

- `merge_chips_after_regen(existing, fresh)` — applied inside the taste
  regen job after the LLM returns a fresh chip list. Preserves confirmed
  chips verbatim; resets rejected chips to pending if the LLM's fresh
  signal_count exceeds the stored value; updates signal_count on pending
  chips; adds genuinely new LLM chips with status=pending.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kebi.core.taste.schemas import Chip, ChipStatus

if TYPE_CHECKING:
    from kebi.api.schemas.signal import ChipConfirmChipItem


def merge_chip_statuses(
    existing: list[Chip],
    submissions: list[ChipConfirmChipItem],
) -> list[Chip]:
    """Merge chip_confirm submissions into the stored chip array.

    Invariants:
    - Already-confirmed chips are never mutated by a chip_confirm.
    - Submitted chips that don't match any stored `(source_field, source_value)`
      are silently ignored (spec edge case).
    - Stored chips not mentioned in the submission are preserved unchanged.
    """
    submissions_by_key = {
        (sub.source_field, sub.source_value): sub for sub in submissions
    }
    merged: list[Chip] = []
    for chip in existing:
        sub = submissions_by_key.get((chip.source_field, chip.source_value))
        if sub is None or chip.status == ChipStatus.CONFIRMED:
            merged.append(chip)
            continue
        merged.append(
            chip.model_copy(
                update={
                    "status": ChipStatus(sub.status),
                    "selection_round": sub.selection_round,
                }
            )
        )
    return merged


def merge_chips_after_regen(
    existing: list[Chip],
    fresh: list[Chip],
) -> list[Chip]:
    """Merge LLM-emitted chips (`fresh`) back into the stored array.

    Rules:
    - Existing confirmed chips are preserved verbatim, even if missing from
      `fresh` (LLM may drop them when signal_counts no longer covers the
      source_field at the chip_threshold).
    - Existing rejected chips are reset to pending with `selection_round=None`
      when the fresh chip's `signal_count` is strictly greater than the
      stored `signal_count` (user may get a chance to reconsider).
    - Existing pending chips get `signal_count` refreshed from `fresh`.
    - Fresh chips not matching any stored chip are appended as pending.
    """
    fresh_by_key = {(c.source_field, c.source_value): c for c in fresh}

    merged: list[Chip] = []
    seen_keys: set[tuple[str, str]] = set()

    for chip in existing:
        key = (chip.source_field, chip.source_value)
        seen_keys.add(key)
        fresh_chip = fresh_by_key.get(key)

        if chip.status == ChipStatus.CONFIRMED:
            merged.append(chip)
            continue

        if chip.status == ChipStatus.REJECTED:
            if fresh_chip is not None and fresh_chip.signal_count > chip.signal_count:
                merged.append(
                    chip.model_copy(
                        update={
                            "status": ChipStatus.PENDING,
                            "selection_round": None,
                            "signal_count": fresh_chip.signal_count,
                            "query": fresh_chip.query,
                        }
                    )
                )
            else:
                merged.append(chip)
            continue

        # pending
        if fresh_chip is not None:
            merged.append(
                chip.model_copy(
                    update={
                        "signal_count": fresh_chip.signal_count,
                        "query": fresh_chip.query,
                    }
                )
            )
        else:
            merged.append(chip)

    for chip in fresh:
        key = (chip.source_field, chip.source_value)
        if key in seen_keys:
            continue
        merged.append(
            chip.model_copy(
                update={"status": ChipStatus.PENDING, "selection_round": None}
            )
        )

    return merged


__all__ = ["merge_chip_statuses", "merge_chips_after_regen"]
