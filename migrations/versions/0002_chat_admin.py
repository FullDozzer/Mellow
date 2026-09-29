"""Chat administration tables (settings, triggers, command access, grid, activity).

Like the initial revision this migration is derived from ``Base.metadata`` and is
idempotent: ``create_all`` only fills in tables that are missing, so it is safe both
for a database created by ``0001_initial`` and for a fresh one where the bot has
already created the schema on startup.

The bot itself only calls ``create_all`` for tables that do not exist yet, so a column
added to a table from the previous revision has to be added here by hand — that is what
``punishments.chat_id`` does.
"""
import sqlalchemy as sa
from alembic import op

from mellow.models import Base

revision = "0002_chat_admin"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

NEW_TABLES = {"chat_settings", "triggers", "command_access", "grid_chats", "chat_members",
              "daily_message_stats", "creator_wills", "user_command_access", "chat_leaves"}

# Columns added to tables of the previous revision, with the index to create for them.
NEW_COLUMNS = {
    "punishments": (
        ("chat_id", sa.Integer(), "ix_punishments_chat_id"),
    ),
    "staff": (
        ("show_online", sa.Boolean(), None),
    ),
    "chat_settings": (
        ("notify_command_access", sa.Boolean(), None),
        ("channels_denied", sa.Boolean(), None),
        ("notify_joins", sa.Boolean(), None),
        ("notify_leaves", sa.Boolean(), None),
        ("leave_notify_min_messages", sa.Integer(), None),
        ("minreg_days", sa.Integer(), None),
        ("closed_permissions", sa.JSON(), None),
        ("autokick_count", sa.Integer(), None),
        ("autokick_window_seconds", sa.Integer(), None),
        ("autokick_action", sa.String(10), None),
        ("auto_join_requests", sa.Boolean(), None),
        ("invite_links", sa.JSON(), None),
    ),
    "chat_members": (
        ("tag", sa.String(16), None),
    ),
}


def _existing_columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _existing_indexes(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {index["name"] for index in inspector.get_indexes(table)}


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade():
    Base.metadata.create_all(bind=op.get_bind(), tables=[Base.metadata.tables[name] for name in NEW_TABLES])
    for table, columns in NEW_COLUMNS.items():
        if not _has_table(table):
            # Таблицы ещё нет (её создаст create_all выше или она появится позже) — колонки не нужны.
            continue
        existing = _existing_columns(table)
        indexes = _existing_indexes(table)
        for column, column_type, index in columns:
            if column in existing:
                continue
            op.add_column(table, sa.Column(column, column_type, nullable=True))
            if index and index not in indexes:
                op.create_index(index, table, [column])


def downgrade():
    for table, columns in NEW_COLUMNS.items():
        if not _has_table(table):
            continue
        existing = _existing_columns(table)
        indexes = _existing_indexes(table)
        for column, _type, index in columns:
            if column not in existing:
                continue
            if index and index in indexes:
                op.drop_index(index, table_name=table)
            op.drop_column(table, column)
    Base.metadata.drop_all(bind=op.get_bind(), tables=[Base.metadata.tables[name] for name in NEW_TABLES])
