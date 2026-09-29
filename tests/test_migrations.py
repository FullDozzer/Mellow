"""The migrations must work on a database of the previous revision, not only on a fresh one.

``alembic upgrade head`` is what runs in production, and the bot's own ``create_all`` only
creates missing tables — it never adds a column to an existing one. These tests therefore
build the old schema by hand and check that the upgrade brings it to the current models.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from mellow.models import Base

REPO_ROOT = Path(__file__).resolve().parent.parent

OLD_PUNISHMENTS = """
CREATE TABLE punishments (
    id INTEGER NOT NULL PRIMARY KEY,
    target_user_id INTEGER NOT NULL,
    moderator_id INTEGER,
    type VARCHAR(20) NOT NULL,
    reason TEXT,
    duration INTEGER,
    created_at DATETIME,
    expires_at DATETIME,
    active BOOLEAN
)
"""


def _tables(database: Path) -> set[str]:
    with sqlite3.connect(database) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {row[0] for row in rows}


def _columns(database: Path, table: str) -> set[str]:
    with sqlite3.connect(database) as connection:
        rows = connection.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in rows}


@pytest.fixture()
def migrations(tmp_path, monkeypatch):
    """Alembic pointed at a temporary database, exactly as the bot's entrypoint does it."""
    database = tmp_path / "mellow.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    monkeypatch.chdir(REPO_ROOT)
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    assert os.environ["DATABASE_URL"].endswith(str(database))
    return config, database


def test_fresh_database_gets_every_table(migrations):
    config, database = migrations

    command.upgrade(config, "head")

    tables = _tables(database)
    assert {"chat_settings", "triggers", "command_access", "grid_chats", "chat_members",
            "daily_message_stats", "punishments", "users"} <= tables
    assert "chat_id" in _columns(database, "punishments")
    for model in (Base.metadata.tables["chat_settings"], Base.metadata.tables["daily_message_stats"]):
        assert {column.name for column in model.columns} <= _columns(database, model.name)


def test_upgrade_repairs_a_database_of_the_previous_revision(migrations):
    config, database = migrations
    with sqlite3.connect(database) as connection:
        connection.execute(OLD_PUNISHMENTS)
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
        connection.execute("INSERT INTO alembic_version (version_num) VALUES ('0001_initial')")

    command.upgrade(config, "head")

    assert "chat_id" in _columns(database, "punishments")
    assert "chat_settings" in _tables(database)
    with sqlite3.connect(database) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert version == "0002_chat_admin"


def test_upgrade_can_run_twice(migrations):
    config, database = migrations

    command.upgrade(config, "head")
    command.upgrade(config, "head")

    assert "chat_id" in _columns(database, "punishments")
    assert "chat_settings" in _tables(database)


def test_downgrade_removes_the_chat_administration_layer(migrations):
    config, database = migrations
    command.upgrade(config, "head")

    command.downgrade(config, "0001_initial")

    tables = _tables(database)
    assert not {"chat_settings", "triggers", "command_access", "grid_chats", "chat_members",
                "daily_message_stats"} & tables
    assert "punishments" in tables
    assert "chat_id" not in _columns(database, "punishments")
