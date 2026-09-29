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
              "daily_message_stats"}

NEW_COLUMNS = {
    "punishments": ("chat_id", sa.Integer(), "ix_punishments_chat_id"),
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


def upgrade():
    Base.metadata.create_all(bind=op.get_bind(), tables=[Base.metadata.tables[name] for name in NEW_TABLES])
    for table, (column, column_type, index) in NEW_COLUMNS.items():
        if column in _existing_columns(table):
            continue
        op.add_column(table, sa.Column(column, column_type, nullable=True))
        if index not in _existing_indexes(table):
            op.create_index(index, table, [column])


def downgrade():
    for table, (column, _type, index) in NEW_COLUMNS.items():
        if column in _existing_columns(table):
            if index in _existing_indexes(table):
                op.drop_index(index, table_name=table)
            op.drop_column(table, column)
    Base.metadata.drop_all(bind=op.get_bind(), tables=[Base.metadata.tables[name] for name in NEW_TABLES])
