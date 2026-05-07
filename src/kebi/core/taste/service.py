"""TasteModelService — signal_counts + LLM summary + chips (ADR-058).

Replaces the former EMA-based taste model. All EMA logic is deleted.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Coroutine
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kebi.core.config import get_config
from kebi.core.taste.aggregation import aggregate_signal_counts
from kebi.core.taste.chip_merge import merge_chips_after_regen
from kebi.core.taste.regen import (
    build_regen_messages,
    validate_grounded,
)
from kebi.core.taste.schemas import (
    Chip,
    ChipView,
    TasteArtifacts,
    TasteContext,
    TasteProfile,
)
from kebi.core.taste.tier import derive_signal_tier, selection_round_name
from kebi.db.models import InteractionType
from kebi.db.repositories.taste_model_repository import (
    SQLAlchemyTasteModelRepository,
)
from kebi.providers.llm import get_llm

logger = logging.getLogger(__name__)


class TasteModelService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._repo = SQLAlchemyTasteModelRepository(session_factory)
        self._config = get_config()

    async def handle_signal(
        self,
        user_id: str,
        signal_type: InteractionType,
        place_id: str,
    ) -> None:
        """Write interaction row, schedule debounced regen."""
        await self._repo.log_interaction(user_id, signal_type, place_id)

        # Import here to avoid circular dependency at module level
        from kebi.core.taste.debounce import regen_debouncer

        def _regen_factory(uid: str = user_id) -> Coroutine[Any, Any, None]:
            return self._run_regen(uid)

        regen_debouncer.schedule(
            user_id=user_id,
            coro_factory=_regen_factory,
            delay_seconds=self._config.taste_model.debounce_window_seconds,
        )

    async def get_taste_profile(self, user_id: str) -> TasteProfile | None:
        """Read taste_model row. No LLM call.

        Hardens against legacy/corrupt JSONB shapes: `chips` and
        `taste_profile_summary` are expected to be arrays but older rows
        occasionally hold `{}` or other non-array values. Rather than 500
        the endpoint, coerce those to empty lists and log a warning so the
        next regen cycle can rebuild them cleanly.
        """
        taste_model = await self._repo.get_by_user_id(user_id)
        if taste_model is None:
            return None

        raw_chips = taste_model.chips
        chips_list: list[Any] = raw_chips if isinstance(raw_chips, list) else []
        if not isinstance(raw_chips, list):
            logger.warning(
                "taste_model.chips for user %s is not a list (got %s) — "
                "coercing to [] until next regen",
                user_id,
                type(raw_chips).__name__,
            )

        raw_summary = taste_model.taste_profile_summary
        summary_list: list[Any] = raw_summary if isinstance(raw_summary, list) else []
        if not isinstance(raw_summary, list):
            logger.warning(
                "taste_model.taste_profile_summary for user %s is not a list "
                "(got %s) — coercing to [] until next regen",
                user_id,
                type(raw_summary).__name__,
            )

        return TasteProfile(
            taste_profile_summary=summary_list,
            signal_counts=taste_model.signal_counts,
            chips=chips_list,
            generated_from_log_count=taste_model.generated_from_log_count,
        )

    async def get_taste_context(self, user_id: str) -> TasteContext:
        """Return the tier + chips slice of GET /v1/user/context.

        The route handler composes this with `saved_places_count` from
        PlacesService — the count never touches this service. Cold users
        (no taste_model row) get tier="cold" and an empty chips array.
        """
        profile = await self.get_taste_profile(user_id)
        stages = self._config.taste_model.chip_selection_stages
        chip_threshold = self._config.taste_model.chip_threshold

        if profile is None:
            return TasteContext(
                signal_tier=derive_signal_tier(0, [], stages, chip_threshold),
                chips=[],
            )

        signal_tier = derive_signal_tier(
            signal_count=profile.generated_from_log_count,
            chips=profile.chips,
            stages=stages,
            chip_threshold=chip_threshold,
        )

        # Stamp still-pending chips with the current crossed-stage name so
        # the frontend can blindly echo `selection_round` back in a
        # chip_confirm submission. Confirmed/rejected chips keep their
        # original round (could be older than the current stage).
        current_sr = selection_round_name(profile.generated_from_log_count, stages)
        chips = [
            ChipView(
                label=chip.label,
                source_field=chip.source_field,
                source_value=chip.source_value,
                signal_count=chip.signal_count,
                query=chip.query,
                status=chip.status,
                selection_round=chip.selection_round or current_sr,
            )
            for chip in profile.chips
        ]

        return TasteContext(
            signal_tier=signal_tier,
            chips=chips,
        )

    async def run_regen_now(self, user_id: str) -> None:
        """Run the regen pipeline immediately, bypassing the debouncer.

        Used by the ChipConfirmed event handler to rewrite the taste
        summary synchronously (well, as a background task per ADR-043)
        after a user submits a chip_confirm — waiting a debounce window
        would make the summary feel stale relative to the explicit action.
        """
        await self._run_regen(user_id, force=True)

    async def _run_regen(self, user_id: str, force: bool = False) -> None:
        """Read interactions -> aggregate -> LLM artifacts -> validate -> write.

        Args:
            user_id: Target user.
            force: If True, skip the stale-guard and min-signals guard.
                Used by chip_confirm rewrites where the signals haven't
                changed but chip statuses have.
        """
        rows = await self._repo.get_interactions_with_places(user_id)

        # Min-signals guard (skipped on force)
        if not force and len(rows) < self._config.taste_model.regen.min_signals:
            return

        signal_counts = aggregate_signal_counts(rows)

        # Stale guard: skip if no new signals since last regen (skipped on force)
        taste_model = await self._repo.get_by_user_id(user_id)
        if (
            not force
            and taste_model
            and taste_model.generated_from_log_count == len(rows)
        ):
            return

        existing_chips = (
            [Chip.model_validate(c) for c in taste_model.chips] if taste_model else []
        )

        # Build prompt and call LLM — feed confirmed/rejected chips through
        # so the prompt can emit assertive/negative sentences (feature 023).
        messages = build_regen_messages(
            signal_counts,
            self._config.taste_model.regen.early_signal_threshold,
            existing_chips=existing_chips,
        )
        artifacts = await self._call_llm_with_retry(messages)
        if artifacts is None:
            logger.warning("Regen skipped for user %s: LLM parse failure", user_id)
            return

        # Validate grounding
        artifacts, dropped = validate_grounded(artifacts, signal_counts)

        # Merge LLM chips back with existing lifecycle state (feature 023):
        # confirmed chips preserved verbatim; rejected resurfaces if signal
        # grew; pending signal_counts refreshed; genuinely new chips added.
        merged_chips = merge_chips_after_regen(existing_chips, artifacts.chips)

        # Langfuse trace metadata
        metadata: dict[str, Any] = {
            "user_id": user_id,
            "log_row_count": len(rows),
            "prior_log_count": (
                taste_model.generated_from_log_count if taste_model else 0
            ),
            "debounce_window_ms": (
                self._config.taste_model.debounce_window_seconds * 1000
            ),
            "forced": force,
        }
        if dropped:
            metadata["dropped_item_count"] = len(dropped)
            metadata["dropped_items"] = dropped

        logger.info(
            "Regen completed for user %s: "
            "%d summary lines, %d chips (%d merged), %d dropped",
            user_id,
            len(artifacts.summary),
            len(artifacts.chips),
            len(merged_chips),
            len(dropped),
        )

        # Persist — repo commits internally
        await self._repo.upsert_regen(
            user_id=user_id,
            signal_counts=signal_counts.model_dump(exclude_defaults=False),
            summary=[line.model_dump() for line in artifacts.summary],
            chips=[chip.model_dump() for chip in merged_chips],
            log_count=len(rows),
        )

    async def _call_llm_with_retry(
        self, messages: list[dict[str, str]]
    ) -> TasteArtifacts | None:
        """Call LLM and parse into TasteArtifacts. Retry once on failure."""
        llm = get_llm("taste_regen")

        for attempt in range(2):
            try:
                raw = await llm.complete(messages)
                parsed = json.loads(raw)
                return TasteArtifacts.model_validate(parsed)
            except (json.JSONDecodeError, ValidationError) as exc:
                if attempt == 0:
                    logger.warning("LLM parse attempt 1 failed, retrying: %s", exc)
                else:
                    logger.error("LLM parse attempt 2 failed, skipping: %s", exc)
                    return None
        return None
