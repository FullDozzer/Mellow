from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from mellow.models import Base


def create_database(database_url: str) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def create_schema(engine: AsyncEngine) -> None:
    """Create any tables that are still missing from the database.

    DDL has to run on an engine/connection: :meth:`sqlalchemy.MetaData.create_all`
    calls ``bind._run_ddl_visitor(...)``, and an ORM ``Session`` does not
    implement it. ``AsyncSession.run_sync(Base.metadata.create_all)`` therefore
    passes the *Session* itself as ``bind`` and raises
    ``AttributeError: 'Session' object has no attribute '_run_ddl_visitor'``.
    ``AsyncConnection.run_sync`` passes a real :class:`Connection` instead, which
    is what ``create_all`` expects.

    Alembic remains the source of truth for the production schema
    (``alembic upgrade head`` runs before the bot). Because ``create_all`` is
    ``checkfirst`` by default, this call is idempotent: after the migrations it
    is a no-op, and it only fills in missing tables for a fresh local/SQLite
    database or tests that have not been migrated.
    """
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
