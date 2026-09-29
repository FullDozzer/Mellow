"""Per-chat settings, command access («Доступ команд») and small shared helpers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mellow.models import ChatSettings, CommandAccess, utcnow
from mellow.services import staff_level

# Commands whose minimum rank can be overridden per chat with «дк <команда> <ранг>».
COMMANDS: dict[str, tuple[int, str]] = {
    "варны": (1, "предупреждения: выдача, снятие, список"),
    "муты": (1, "муты: список и проверка"),
    "чистка": (2, "удаление сообщений, кик неактива, новичков и удалённых"),
    "настройки": (3, "фильтры, лимиты, приветствие и правила чата"),
    "триггеры": (4, "автоматические наказания"),
    "сетка": (4, "установка и команды сетки чатов"),
    "чаты": (1, "просмотр списка чатов сетки"),
    "статистика": (1, "статистика чата и участников"),
    "модер": (5, "назначение и снятие рангов"),
}

COMMAND_ALIASES = {
    "унб": "триггеры", "триги": "триггеры", "триггер": "триггеры",
    "настройка": "настройки", "настройка чата": "настройки",
    "сетка чатов": "сетка", "стата": "статистика", "статистика чата": "статистика",
    "модерация": "модер", "ранги": "модер", "варн": "варны", "мут": "муты",
    "чистка смс": "чистка", "удалить": "чистка", "кик": "чистка",
}

DEFAULT_LEVEL_WHEN_UNKNOWN = 5


def command_key(raw: str) -> str | None:
    value = " ".join((raw or "").lower().split())
    if value in COMMANDS:
        return value
    return COMMAND_ALIASES.get(value)


@dataclass(frozen=True)
class ChatConfig:
    chat_id: int
    welcome_text: str | None = None
    rules_text: str | None = None
    warning_limit: int = 3
    warning_ban_seconds: int = 604800
    warning_period_seconds: int | None = None
    mute_default_seconds: int = 604800
    ban_default_seconds: int | None = None
    links_denied: bool = False
    denied_link_types: list[str] = field(default_factory=list)
    allowed_links: list[str] = field(default_factory=list)
    caps_percent: int | None = None
    caps_min_length: int = 5
    sticker_limit: int | None = None
    voice_denied: bool = False
    guest_bots_denied: bool = False
    profanity_filter: bool = False
    show_charts: bool = True
    show_mod_tags: bool = False

    @classmethod
    def from_row(cls, row: ChatSettings) -> "ChatConfig":
        return cls(
            chat_id=row.chat_id, welcome_text=row.welcome_text, rules_text=row.rules_text,
            warning_limit=row.warning_limit, warning_ban_seconds=row.warning_ban_seconds,
            warning_period_seconds=row.warning_period_seconds, mute_default_seconds=row.mute_default_seconds,
            ban_default_seconds=row.ban_default_seconds, links_denied=row.links_denied,
            denied_link_types=list(row.denied_link_types or []), allowed_links=list(row.allowed_links or []),
            caps_percent=row.caps_percent, caps_min_length=row.caps_min_length,
            sticker_limit=row.sticker_limit, voice_denied=row.voice_denied,
            guest_bots_denied=row.guest_bots_denied, profanity_filter=row.profanity_filter,
            show_charts=row.show_charts, show_mod_tags=row.show_mod_tags,
        )


async def chat_config(session: AsyncSession, chat_id: int) -> ChatConfig:
    """Read a chat's settings; a chat that was never configured gets the defaults."""
    row = await session.get(ChatSettings, chat_id)
    return ChatConfig.from_row(row) if row is not None else ChatConfig(chat_id=chat_id)


class ChatSettingsStore:
    """A tiny TTL cache in front of ``chat_settings``.

    The chat guard consults settings for every group message, so the row is cached for
    a few seconds; every write through this store invalidates the cache immediately.
    """

    def __init__(self, session_factory, ttl_seconds: float = 30.0):
        self._session_factory = session_factory
        self._ttl = ttl_seconds
        self._cache: dict[int, tuple[float, ChatConfig]] = {}

    def invalidate(self, chat_id: int | None = None) -> None:
        if chat_id is None:
            self._cache.clear()
        else:
            self._cache.pop(chat_id, None)

    async def get(self, chat_id: int) -> ChatConfig:
        cached = self._cache.get(chat_id)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        async with self._session_factory() as session:
            config = await chat_config(session, chat_id)
        self._cache[chat_id] = (now, config)
        return config

    async def update(self, chat_id: int, **fields) -> ChatConfig:
        """Write settings and return the fresh snapshot; ``None`` removes a value."""
        async with self._session_factory() as session, session.begin():
            row = await session.get(ChatSettings, chat_id)
            if row is None:
                row = ChatSettings(chat_id=chat_id)
                session.add(row)
            for name, value in fields.items():
                if not hasattr(row, name):
                    raise AttributeError(name)
                setattr(row, name, value)
            row.updated_at = utcnow()
            await session.flush()
            config = ChatConfig.from_row(row)
        self._cache[chat_id] = (time.monotonic(), config)
        return config

    async def remember_title(self, chat_id: int, title: str | None) -> None:
        if not title:
            return
        async with self._session_factory() as session, session.begin():
            row = await session.get(ChatSettings, chat_id)
            if row is None:
                session.add(ChatSettings(chat_id=chat_id, title=title))
            elif row.title != title:
                row.title = title
        self.invalidate(chat_id)


async def command_min_level(session: AsyncSession, chat_id: int, key: str) -> int:
    override = await session.scalar(select(CommandAccess.min_level)
                                    .where(CommandAccess.chat_id == chat_id, CommandAccess.command == key))
    if override is not None:
        return int(override)
    entry = COMMANDS.get(key)
    return entry[0] if entry else DEFAULT_LEVEL_WHEN_UNKNOWN


async def set_command_access(session: AsyncSession, chat_id: int, key: str, level: int | None) -> None:
    if level is None:
        await session.execute(sql_delete(CommandAccess)
                              .where(CommandAccess.chat_id == chat_id, CommandAccess.command == key))
        return
    row = await session.get(CommandAccess, (chat_id, key))
    if row is None:
        session.add(CommandAccess(chat_id=chat_id, command=key, min_level=level))
    else:
        row.min_level = level
        row.updated_at = utcnow()


async def may_use(session: AsyncSession, chat_id: int, telegram_id: int, key: str) -> tuple[bool, int, int]:
    """``(allowed, actor_level, required_level)`` for a command in a given chat."""
    level = await staff_level(session, telegram_id)
    required = await command_min_level(session, chat_id, key)
    return level >= required and level > 0, level, required


def describe_access(key: str, required: int, default: int) -> str:
    title = COMMANDS.get(key, (default, ""))[1]
    marker = "" if required == default else " (изменено)"
    return f"<code>{key}</code> — от {required} уровня{marker}: {title}"


def with_defaults(config: ChatConfig, **fields) -> ChatConfig:
    return replace(config, **fields)
