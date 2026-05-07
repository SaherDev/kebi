"""PlacesRepository — the only code that reads or writes the `places` ORM.

Every other service in the repo consumes `PlaceObject` (Pydantic) instances.
The namespaced `provider_id` string is constructed only here (`_build_provider_id`)
and parsed only in `PlacesService` (`_strip_namespace`).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from kebi.core.places.models import (
    DuplicatePlaceError,
    DuplicateProviderId,
    PlaceAttributes,
    PlaceCreate,
    PlaceObject,
    PlaceProvider,
    PlaceSource,
    PlaceType,
)
from kebi.db.models import Place

logger = logging.getLogger(__name__)


def build_provider_id(
    provider: PlaceProvider | None, external_id: str | None
) -> str | None:
    """Build a namespaced provider_id string: ``{provider}:{external_id}``.

    This is the ONLY construction site for the provider_id namespace in the
    codebase. Every call site outside this module imports this function
    instead of inlining the f-string, so future namespace changes live in
    one place. The counterpart parser lives in `PlacesService._strip_namespace`.
    """
    if provider is None or external_id is None:
        return None
    return f"{provider.value}:{external_id}"


class PlacesRepository:
    """Async repository over the `places` table (ADR-054, feature 019)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Provider-id namespace — the ONLY construction site in the codebase.
    # PlacesService._strip_namespace is the ONLY parser. Do not inline.
    # Callers outside this module use the module-level `build_provider_id`
    # helper (alias of the same function) so all namespace construction
    # flows through this one implementation.
    # ------------------------------------------------------------------
    @staticmethod
    def _build_provider_id(
        provider: PlaceProvider | None, external_id: str | None
    ) -> str | None:
        return build_provider_id(provider, external_id)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    async def create(self, data: PlaceCreate) -> PlaceObject:
        provider_id = self._build_provider_id(data.provider, data.external_id)
        row = self._to_insert_values(data, provider_id)

        stmt = (
            insert(Place)
            .values(**row)
            .returning(
                Place.id,
                Place.user_id,
                Place.place_name,
                Place.place_type,
                Place.subcategory,
                Place.tags,
                Place.attributes,
                Place.source_url,
                Place.source,
                Place.provider_id,
                Place.created_at,
            )
        )

        try:
            result = await self._session.execute(stmt)
            returned = result.one()
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            existing_id = await self._lookup_place_id_by_provider_id(provider_id)
            conflict = DuplicateProviderId(
                provider_id=provider_id or "",
                existing_place_id=existing_id or "",
            )
            logger.warning(
                "places.create.duplicate",
                extra={
                    "provider_id": provider_id,
                    "existing_place_id": existing_id,
                },
            )
            raise DuplicatePlaceError([conflict]) from None
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise RuntimeError(f"PlacesRepository.create failed: {exc}") from exc

        return self._row_to_place_object(returned._mapping)

    async def create_batch(self, items: list[PlaceCreate]) -> list[PlaceObject]:
        if not items:
            return []

        rows = [
            self._to_insert_values(
                item, self._build_provider_id(item.provider, item.external_id)
            )
            for item in items
        ]

        stmt = (
            insert(Place)
            .values(rows)
            .returning(
                Place.id,
                Place.user_id,
                Place.place_name,
                Place.place_type,
                Place.subcategory,
                Place.tags,
                Place.attributes,
                Place.source_url,
                Place.source,
                Place.provider_id,
                Place.created_at,
            )
        )

        try:
            result = await self._session.execute(stmt)
            returned_rows = result.all()
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            conflicts = await self._collect_batch_conflicts(items)
            logger.warning(
                "places.create_batch.duplicate",
                extra={
                    "provider_ids": [c.provider_id for c in conflicts],
                    "count": len(conflicts),
                },
            )
            raise DuplicatePlaceError(conflicts) from None
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise RuntimeError(f"PlacesRepository.create_batch failed: {exc}") from exc

        return [self._row_to_place_object(row._mapping) for row in returned_rows]

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    async def get(self, place_id: str) -> PlaceObject | None:
        try:
            result = await self._session.execute(
                select(Place).where(Place.id == place_id)
            )
            row = result.scalar_one_or_none()
        except SQLAlchemyError as exc:
            raise RuntimeError(f"PlacesRepository.get failed: {exc}") from exc
        if row is None:
            return None
        return self._orm_to_place_object(row)

    async def get_by_external_id(
        self, provider: PlaceProvider, external_id: str
    ) -> PlaceObject | None:
        provider_id = self._build_provider_id(provider, external_id)
        try:
            result = await self._session.execute(
                select(Place).where(Place.provider_id == provider_id)
            )
            row = result.scalar_one_or_none()
        except SQLAlchemyError as exc:
            raise RuntimeError(
                f"PlacesRepository.get_by_external_id failed: {exc}"
            ) from exc
        if row is None:
            return None
        return self._orm_to_place_object(row)

    async def count_for_user(self, user_id: str) -> int:
        try:
            result = await self._session.execute(
                select(func.count()).select_from(Place).where(Place.user_id == user_id)
            )
            return int(result.scalar_one() or 0)
        except SQLAlchemyError as exc:
            raise RuntimeError(
                f"PlacesRepository.count_for_user failed: {exc}"
            ) from exc

    async def get_batch(self, place_ids: list[str]) -> list[PlaceObject]:
        if not place_ids:
            return []
        try:
            result = await self._session.execute(
                select(Place).where(Place.id.in_(place_ids))
            )
            rows = result.scalars().all()
        except SQLAlchemyError as exc:
            raise RuntimeError(f"PlacesRepository.get_batch failed: {exc}") from exc
        by_id: dict[str, Place] = {row.id: row for row in rows}
        return [
            self._orm_to_place_object(by_id[pid]) for pid in place_ids if pid in by_id
        ]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _lookup_place_id_by_provider_id(
        self, provider_id: str | None
    ) -> str | None:
        if provider_id is None:
            return None
        try:
            result = await self._session.execute(
                select(Place.id).where(Place.provider_id == provider_id)
            )
            value = result.scalar_one_or_none()
        except SQLAlchemyError:
            return None
        return value

    async def _collect_batch_conflicts(
        self, items: list[PlaceCreate]
    ) -> list[DuplicateProviderId]:
        conflicts: list[DuplicateProviderId] = []
        for item in items:
            provider_id = self._build_provider_id(item.provider, item.external_id)
            if provider_id is None:
                continue
            existing_id = await self._lookup_place_id_by_provider_id(provider_id)
            if existing_id is not None:
                conflicts.append(
                    DuplicateProviderId(
                        provider_id=provider_id,
                        existing_place_id=existing_id,
                    )
                )
        return conflicts

    @staticmethod
    def _to_insert_values(data: PlaceCreate, provider_id: str | None) -> dict[str, Any]:
        return {
            "id": str(uuid4()),
            "user_id": data.user_id,
            "place_name": data.place_name,
            "place_type": data.place_type.value,
            "subcategory": data.subcategory,
            "tags": list(data.tags) if data.tags else None,
            "attributes": data.attributes.model_dump(exclude_none=False),
            "source_url": data.source_url,
            "source": data.source.value if data.source else None,
            "provider_id": provider_id,
        }

    @staticmethod
    def _row_to_place_object(mapping: Any) -> PlaceObject:
        attributes_raw = mapping["attributes"]
        attributes = (
            PlaceAttributes.model_validate(attributes_raw)
            if attributes_raw
            else PlaceAttributes()
        )
        source_raw = mapping["source"]
        source = PlaceSource(source_raw) if source_raw else None
        return PlaceObject(
            place_id=mapping["id"],
            place_name=mapping["place_name"],
            place_type=PlaceType(mapping["place_type"]),
            subcategory=mapping["subcategory"],
            tags=list(mapping["tags"]) if mapping["tags"] else [],
            attributes=attributes,
            source_url=mapping["source_url"],
            source=source,
            provider_id=mapping["provider_id"],
            created_at=mapping.get("created_at"),
        )

    @staticmethod
    def _orm_to_place_object(row: Place) -> PlaceObject:
        attributes = (
            PlaceAttributes.model_validate(row.attributes)
            if row.attributes
            else PlaceAttributes()
        )
        source = PlaceSource(row.source) if row.source else None
        return PlaceObject(
            place_id=row.id,
            place_name=row.place_name,
            place_type=PlaceType(row.place_type),
            subcategory=row.subcategory,
            tags=list(row.tags) if row.tags else [],
            attributes=attributes,
            source_url=row.source_url,
            source=source,
            provider_id=row.provider_id,
            created_at=row.created_at,
        )
