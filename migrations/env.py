from logging.config import fileConfig
import asyncio
import os
from dotenv import load_dotenv
from alembic import context

load_dotenv()
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from mellow.models import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def database_url():
    """Runtime URL as used by the bot: migrations run on the async driver (asyncpg/aiosqlite).

    The project only ships async drivers, so the URL must keep its ``+asyncpg`` /
    ``+aiosqlite`` suffix — dropping it would make SQLAlchemy load psycopg, which
    is not installed in the Docker image.
    """
    return os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./mellow.db")


def offline_database_url():
    """``--sql`` renders statements without connecting, and cannot use an async driver."""
    return database_url().replace("+aiosqlite", "").replace("+asyncpg", "")


def do_run_migrations(connection: Connection):
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline():
    context.configure(url=offline_database_url(), target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations():
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = database_url()
    connectable = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online():
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
