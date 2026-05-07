"""Signal-tier derivation (feature 023).

Pure function. No I/O, no persistence. Computed on demand from the user's
signal count and chip state and consumed by GET /v1/user/context and by any
future agent context node.

Stages are driven entirely by `config.taste_model.chip_selection_stages` —
a `dict[str, int]` mapping stage name to signal-count threshold. Adding or
removing a stage in config requires zero code changes here.
"""

from __future__ import annotations

from kebi.core.taste.schemas import Chip, ChipStatus, SignalTier


def derive_signal_tier(
    signal_count: int,
    chips: list[Chip],
    stages: dict[str, int],
    chip_threshold: int,
) -> SignalTier:
    """Derive the user's signal tier from their current state.

    Rules (spec FR-003, clarified 2026-04-18):
      count == 0                                          -> cold
      count < lowest crossed stage threshold              -> warming
      any actionable pending chip (status=pending,
        signal_count >= chip_threshold,
        selection_round is None)                          -> chip_selection
      otherwise (count has crossed a stage, no actionable
        pending chips remain)                             -> active

    Args:
        signal_count: Number of interactions backing the current taste model.
            Typically `taste_model.generated_from_log_count`.
        chips: Current chip array (status + selection_round carried on each).
        stages: `chip_selection_stages` config dict, e.g. {"round_1": 5, "round_2": 20}.
        chip_threshold: Minimum per-chip signal_count for a pending chip to be
            considered actionable (i.e. ready to be shown to the user).

    Returns:
        One of "cold" | "warming" | "chip_selection" | "active".
    """
    if signal_count == 0:
        return "cold"

    crossed = [threshold for threshold in stages.values() if signal_count >= threshold]
    if not crossed:
        return "warming"

    actionable_pending = [
        c
        for c in chips
        if c.status == ChipStatus.PENDING
        and c.signal_count >= chip_threshold
        and c.selection_round is None
    ]
    if actionable_pending:
        return "chip_selection"

    return "active"


def selection_round_name(signal_count: int, stages: dict[str, int]) -> str | None:
    """Return the stage name the frontend should submit under (feature 023).

    Derives the highest-threshold stage in `chip_selection_stages` that the
    user has crossed. The frontend reads this from `/v1/user/context` as the
    top-level `selection_round` field and copies it into
    `metadata.selection_round` when building a `chip_confirm` submission —
    the server stamps it onto each matched chip during merge.

    Returns None when no stage has been crossed (tier=cold/warming) — the
    route handler also returns None when tier=active, since nothing is
    pending.
    """
    crossed = [
        (name, threshold)
        for name, threshold in stages.items()
        if signal_count >= threshold
    ]
    if not crossed:
        return None
    return max(crossed, key=lambda pair: pair[1])[0]
