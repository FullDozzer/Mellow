from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from mellow.db import create_database, create_schema

EXPECTED_TABLES = {"users", "staff", "applications", "outbox_events", "processed_updates"}


async def _table_names(engine):
    async with engine.connect() as connection:
        return set(await connection.run_sync(lambda sync_connection: inspect(sync_connection).get_table_names()))


async def test_create_schema_creates_tables_on_a_fresh_database(tmp_path):
    engine, session_factory = create_database(f"sqlite+aiosqlite:///{tmp_path / 'mellow.db'}")
    try:
        # Regression: this used to call `session.run_sync(Base.metadata.create_all)`,
        # which binds an ORM Session as the DDL target and raises
        # AttributeError: 'Session' object has no attribute '_run_ddl_visitor'.
        await create_schema(engine)
        assert EXPECTED_TABLES <= await _table_names(engine)

        async with session_factory() as session:
            assert await session.scalar(text("SELECT 1")) == 1
    finally:
        await engine.dispose()


async def test_create_schema_is_idempotent_after_alembic(tmp_path):
    """The bot starts right after `alembic upgrade head`, so create_all must be a no-op."""
    engine, _ = create_database(f"sqlite+aiosqlite:///{tmp_path / 'mellow.db'}")
    try:
        await create_schema(engine)
        tables_after_first_run = await _table_names(engine)
        await create_schema(engine)
        assert await _table_names(engine) == tables_after_first_run
    finally:
        await engine.dispose()


async def test_create_database_returns_engine_bound_to_its_session_factory(tmp_path):
    engine, session_factory = create_database(f"sqlite+aiosqlite:///{tmp_path / 'mellow.db'}")
    try:
        assert isinstance(engine, AsyncEngine)
        assert session_factory.kw["bind"] is engine
    finally:
        await engine.dispose()
