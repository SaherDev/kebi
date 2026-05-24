"""TasteModelService — signal_counts + LLM summary (ADR-077).

Signals are aggregated against the shared places catalog identity;
place data is resolved through the source-of-truth service's DB-only
analytical read plus the per-user save record.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kebi.core.agent._trace_context import feature_trace, traced_call
from kebi.core.config import get_config
from kebi.core.places.protocols import (
    PlacesSearchServiceProtocol,
    UserPlacesRepoProtocol,
)
from kebi.core.taste.aggregation import aggregate_signal_counts
from kebi.core.taste.mapping import place_to_interaction_row
from kebi.core.taste.regen import (
    build_regen_messages,
    validate_grounded,
)
from kebi.core.taste.schemas import (
    InteractionRow,
    RawInteraction,
    TasteArtifacts,
    TasteProfile,
)
from kebi.db.models import InteractionType
from kebi.db.repositories.taste_model_repository import (
    SQLAlchemyTasteModelRepository,
)
from kebi.providers.llm import get_llm

logger = logging.getLogger(__name__)


class TasteModelService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        search_service_factory: Callable[
            [AsyncSession], PlacesSearchServiceProtocol
        ],
        user_places_repo_factory: Callable[[AsyncSession], UserPlacesRepoProtocol],
    ) -> None:
        self._repo = SQLAlchemyTasteModelRepository(session_factory)
        self._session_factory = session_factory
        self._search_service_factory = search_service_factory
        self._user_places_repo_factory = user_places_repo_factory
        self._config = get_config()

    async def handle_signal(
        self,
        user_id: str,
        signal_type: InteractionType,
        place_core_id: str,
    ) -> None:
        """Write interaction row, schedule debounced regen."""
        await self._repo.log_interaction(user_id, signal_type, place_core_id)

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

        Hardens against legacy/corrupt JSONB shapes: `taste_profile_summary`
        is expected to be an array but older rows occasionally hold `{}` or
        other non-array values. Rather than 500 the endpoint, coerce those
        to an empty list and log a warning so the next regen cycle can
        rebuild it cleanly.
        """
        taste_model = await self._repo.get_by_user_id(user_id)
        if taste_model is None:
            return None

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
            generated_from_log_count=taste_model.generated_from_log_count,
        )

    async def _resolve_rows(
        self, user_id: str, raw: list[RawInteraction]
    ) -> list[InteractionRow]:
        """Resolve raw interactions against the places catalog (ADR-077).

        DB-only via the source-of-truth service's analytical read; no Google
        fallback, no cache mutation. Save source comes from the per-user
        save record. Interactions whose place_core_id no longer resolves
        (TTL-wiped / orphaned) are skipped.
        """
        place_core_ids = list({r.place_core_id for r in raw if r.place_core_id})
        async with self._session_factory() as session:
            search = self._search_service_factory(session)
            user_places_repo = self._user_places_repo_factory(session)
            cores = await search.get_cores_by_ids(place_core_ids)
            user_places = await user_places_repo.get_by_user(user_id)

        # user_places.place_id holds the same places.id value (FK).
        source_by_core_id = {up.place_id: up.source.value for up in user_places}
        rows: list[InteractionRow] = []
        for r in raw:
            if not r.place_core_id:
                continue
            core = cores.get(r.place_core_id)
            if core is None:
                continue  # orphan / TTL-wiped place — skip
            rows.append(
                place_to_interaction_row(
                    r.type, core, source_by_core_id.get(r.place_core_id)
                )
            )
        return rows

    async def _run_regen(self, user_id: str) -> None:
        """Read interactions -> resolve -> aggregate -> LLM -> validate -> write."""
        raw = await self._repo.get_interactions(user_id)

        # Min-signals guard
        if len(raw) < self._config.taste_model.regen.min_signals:
            return

        # Stale guard: skip if no new signals since last regen (before the
        # place-resolution reads — nothing to recompute when unchanged).
        taste_model = await self._repo.get_by_user_id(user_id)
        if taste_model and taste_model.generated_from_log_count == len(raw):
            return

        rows = await self._resolve_rows(user_id, raw)
        signal_counts = aggregate_signal_counts(rows)

        # Build prompt and call LLM
        messages = build_regen_messages(
            signal_counts,
            self._config.taste_model.regen.early_signal_threshold,
        )
        artifacts = await self._call_llm_with_retry(messages, user_id)
        if artifacts is None:
            logger.warning("Regen skipped for user %s: LLM parse failure", user_id)
            return

        # Validate grounding
        artifacts, dropped = validate_grounded(artifacts, signal_counts)

        # Langfuse trace metadata
        metadata: dict[str, Any] = {
            "user_id": user_id,
            "log_row_count": len(raw),
            "resolved_row_count": len(rows),
            "prior_log_count": (
                taste_model.generated_from_log_count if taste_model else 0
            ),
            "debounce_window_ms": (
                self._config.taste_model.debounce_window_seconds * 1000
            ),
        }
        if dropped:
            metadata["dropped_item_count"] = len(dropped)
            metadata["dropped_items"] = dropped

        logger.info(
            "Regen completed for user %s: %d summary lines, %d dropped",
            user_id,
            len(artifacts.summary),
            len(dropped),
        )

        # Persist — repo commits internally
        await self._repo.upsert_regen(
            user_id=user_id,
            signal_counts=signal_counts.model_dump(exclude_defaults=False),
            summary=[line.model_dump() for line in artifacts.summary],
            log_count=len(raw),
        )

    async def _call_llm_with_retry(
        self, messages: list[dict[str, str]], user_id: str
    ) -> TasteArtifacts | None:
        """Call LLM and parse into TasteArtifacts. Retry once on failure.

        Standalone Langfuse trace (debounced background work — triggering
        turn is arbitrary). Per-attempt spans so a regen that succeeded on
        the second try shows as ERROR + OK, not a single cheap call.
        """
        llm = get_llm("taste_regen")
        async with feature_trace("taste_regen", user_id):
            for attempt in range(2):
                async with traced_call(
                    "taste_regen.llm",
                    "taste_regen",
                    role="taste_regen",
                    user_id=user_id,
                    extra={"attempt": attempt + 1},
                ) as t:
                    raw = await llm.complete(messages)
                    try:
                        artifacts = TasteArtifacts.model_validate(json.loads(raw))
                    except (json.JSONDecodeError, ValidationError) as exc:
                        t.fail(exc)
                        if attempt == 0:
                            logger.warning(
                                "LLM parse attempt 1 failed, retrying: %s", exc
                            )
                        else:
                            logger.error(
                                "LLM parse attempt 2 failed, skipping: %s", exc
                            )
                            return None
                        continue
                    t.output = {"text": raw}
                    return artifacts
            return None
