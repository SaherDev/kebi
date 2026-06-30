"""ConsultQuotaService — Redis-backed daily consult cap."""

from __future__ import annotations

from unittest.mock import AsyncMock

from kebi.core.chat.consult_quota import ConsultQuotaService


def _redis(incr_returns: int) -> AsyncMock:
    redis = AsyncMock()
    redis.incr = AsyncMock(return_value=incr_returns)
    redis.expire = AsyncMock()
    return redis


async def test_none_limit_is_unlimited_and_skips_redis() -> None:
    redis = _redis(incr_returns=999)
    svc = ConsultQuotaService(redis=redis)

    assert await svc.check_and_increment("user_x", None) is True
    redis.incr.assert_not_called()


async def test_under_limit_is_allowed_and_increments() -> None:
    redis = _redis(incr_returns=1)
    svc = ConsultQuotaService(redis=redis)

    assert await svc.check_and_increment("user_x", 3) is True
    redis.incr.assert_awaited_once()
    # First hit of the day sets the TTL.
    redis.expire.assert_awaited_once()


async def test_at_limit_is_allowed() -> None:
    redis = _redis(incr_returns=3)
    svc = ConsultQuotaService(redis=redis)

    assert await svc.check_and_increment("user_x", 3) is True
    # Not the first hit → no TTL reset.
    redis.expire.assert_not_called()


async def test_over_limit_is_denied() -> None:
    redis = _redis(incr_returns=4)
    svc = ConsultQuotaService(redis=redis)

    assert await svc.check_and_increment("user_x", 3) is False


async def test_redis_failure_fails_open() -> None:
    redis = AsyncMock()
    redis.incr = AsyncMock(side_effect=RuntimeError("redis down"))
    svc = ConsultQuotaService(redis=redis)

    # An infra blip must not block a paying user.
    assert await svc.check_and_increment("user_x", 3) is True
