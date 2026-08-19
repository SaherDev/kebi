from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from kebi.core.config import get_env

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = get_env().DATABASE_URL
        # Ensure asyncpg driver is used
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        # A session that escapes its `async with` — a cancelled request is the
        # usual way — leaves a connection idle inside an open transaction,
        # holding AccessShareLock on whatever it read. The next migration's
        # ALTER TABLE then queues behind it forever, and because the lock queue
        # is FIFO every subsequent reader queues behind the ALTER: one leaked
        # session takes the table down. Observed in production on `places`,
        # where the holder had been idle four days. The server-side timeout is
        # the backstop that turns that outage into a reaped connection; no
        # legitimate query here holds a transaction open for a minute.
        _engine = create_async_engine(
            url,
            echo=False,
            pool_pre_ping=True,
            connect_args={
                "server_settings": {"idle_in_transaction_session_timeout": "60000"}
            },
        )
    return _engine


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(_get_engine(), expire_on_commit=False)
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with _get_session_factory()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
