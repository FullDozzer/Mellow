"""Per-chat settings, command access («Доступ команд») and small shared helpers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mellow.models import ChatSettings, CommandAccess, UserCommandAccess, utcnow
from mellow.services import staff_level

# Commands whose minimum rank can be overridden per chat with «дк <команда> <ранг>».
# Names follow the Iris documentation, so «дк бан 2» limits exactly the ban command.
COMMANDS: dict[str, tuple[int, str]] = {
    "варны": (1, "предупреждения: выдача, снятие, список"),
    "муты": (1, "муты: выдача, снятие, список и проверка"),
    "баны": (1, "бан, разбан, причина, банлист и амнистия"),
    "кик": (1, "исключение участника"),
    "чистка": (2, "удаление сообщений, кик неактива, новичков и удалённых"),
    "настройки": (3, "фильтры, лимиты, приветствие и правила чата"),
    "триггеры": (4, "автоматические наказания"),
    "сетка": (4, "установка и команды сетки чатов"),
    "чаты": (1, "просмотр списка чатов сетки"),
    "статистика": (1, "статистика чата и участников"),
    "модер": (5, "назначение и снятие рангов"),
    "доступ": (5, "настройка доступа команд"),
    "завещание": (5, "наследство и передача создателя"),
    "лдк": (5, "личный доступ команд"),
    # Доступна всем по умолчанию, но её ранг тоже можно поднять: «дк мдк 2».
    "мой дк": (0, "личный доступ: просмотр своих команд"),
    "анкета": (0, "своя анкета: ник, звание, девиз, описание, гражданство"),
    "профиль": (5, "назначение ника, звания и описания участникам"),
}

# Уровень 6 отключает команду совсем, уровень 0 открывает её всем.
DISABLED_LEVEL = 6
PUBLIC_LEVEL = 0

COMMAND_ALIASES = {
    "унб": "триггеры", "триги": "триггеры", "триггер": "триггеры",
    "настройка": "настройки", "настройка чата": "настройки",
    "сетка чатов": "сетка", "стата": "статистика", "статистика чата": "статистика",
    "модерация": "модер", "ранги": "модер",
    "варн": "варны", "выдача варнов": "варны", "снятие варнов": "варны",
    "предупреждения пользователя": "варны", "варнлист": "варны",
    "мут": "муты", "выдача мута": "муты", "снятие мута": "муты", "проверить мут": "муты",
    "список мутов": "муты",
    "бан": "баны", "чс": "баны", "разбан": "баны", "вернуть": "баны", "банлист": "баны",
    "причина бана": "баны", "амнистия": "баны",
    "исключить": "кик",
    "чистка смс": "чистка", "удалить": "чистка", "смс": "чистка", "пург": "чистка",
    "кик неактив": "чистка", "кик актив": "чистка", "кик новичков": "чистка",
    "кик удалённых": "чистка", "кик молчунов": "чистка", "кик по смс": "чистка",
    "кик по сообщениям": "чистка", "кто удалён": "чистка", "кто собака": "чистка",
    "закреп": "настройки", "открепить": "настройки", "название": "настройки",
    "описание чата": "настройки", "правила": "настройки", "приветствие": "настройки",
    "тг админ": "настройки", "тг тег": "настройки", "минрег": "настройки",
    "автокик": "настройки", "автозаявки": "настройки", "каналы": "настройки",
    "входы": "настройки", "выходы": "настройки", "проверить в чате": "настройки",
    "вызов админов": "модер", "кто админ": "модер", "повысить": "модер",
    "модер лог": "модер",
    "вызов дк": "доступ", "дк": "доступ", "мой дк": "доступ", "лдк": "доступ",
    "лог дк": "доступ", "сброс команд": "доступ",
    "передать создателя": "завещание", "завещание": "завещание", "наследство": "завещание",
    "установка сетки": "сетка", "глобан": "сетка", "глоразбан": "сетка",
    "глмодер": "сетка", "гладмин": "сетка", "сетка кик": "сетка", "сетка баны": "сетка",
    "сетка модеры": "сетка", "сетка дк": "сетка", "сетка передать создателя": "сетка",
    "все лдк": "лдк", "сброс лдк": "лдк",
    "сброс всех лдк": "лдк", "входы-выходы": "настройки", "прикрепить": "настройки",
    "снять мут": "муты", "говори": "муты", "закрепить": "настройки",
    "чат ссылка": "настройки", "топик название": "настройки", "сброс ссылок": "настройки",
    "тг права": "настройки", "тг разрешения чата": "настройки", "чат-ссылка": "настройки",
    "ник": "анкета", "звание": "анкета", "девиз": "анкета", "описание": "анкета",
    "гражданство": "анкета", "профиль": "анкета", "установить ник": "профиль",
    "установить звание": "профиль", "установить описание": "профиль",
}

DEFAULT_LEVEL_WHEN_UNKNOWN = 5

# Заполняется модулями команд: ключ команды -> его раздел «Доступ команд».
COMMAND_GROUPS: dict[str, str] = {}


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
    notify_command_access: bool = True
    channels_denied: bool = False
    notify_joins: bool = False
    notify_leaves: bool = False
    leave_notify_min_messages: int = 0
    minreg_days: int | None = None
    closed_permissions: dict | None = None
    autokick_count: int | None = None
    autokick_window_seconds: int | None = None
    autokick_action: str | None = None
    auto_join_requests: bool = False
    # «+Чат ссылка»: ссылки, созданные ботом, чтобы «сброс ссылок» мог их отозвать.
    invite_links: list[str] = field(default_factory=list)
    bots_denied: bool = False
    inline_notices: bool = False

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
            notify_command_access=row.notify_command_access, channels_denied=row.channels_denied,
            notify_joins=row.notify_joins, notify_leaves=row.notify_leaves,
            leave_notify_min_messages=row.leave_notify_min_messages, minreg_days=row.minreg_days,
            closed_permissions=row.closed_permissions, autokick_count=row.autokick_count,
            autokick_window_seconds=row.autokick_window_seconds, autokick_action=row.autokick_action,
            auto_join_requests=row.auto_join_requests,
            invite_links=list(row.invite_links or []),
            bots_denied=row.bots_denied, inline_notices=row.inline_notices,
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
    """The rank a command needs: its own override, its group's override, then the default."""
    overrides = dict((await session.execute(select(CommandAccess.command, CommandAccess.min_level)
                                            .where(CommandAccess.chat_id == chat_id))).all())
    group = key_group_of(key)
    if key in overrides:
        return int(overrides[key])
    if group and group in overrides:
        return int(overrides[group])
    entry = COMMANDS.get(key) or COMMANDS.get(group or "")
    return entry[0] if entry else DEFAULT_LEVEL_WHEN_UNKNOWN


async def personal_access(session: AsyncSession, chat_id: int, telegram_id: int,
                          key: str) -> bool | None:
    """«Лдк»: ``True``/``False`` when the person has an exception for this command."""
    row = await session.scalar(select(UserCommandAccess)
                               .where(UserCommandAccess.chat_id == chat_id,
                                      UserCommandAccess.telegram_id == telegram_id,
                                      UserCommandAccess.command == key))
    if row is not None:
        return bool(row.allowed)
    group = key_group_of(key)
    if group and group != key:
        row = await session.scalar(select(UserCommandAccess)
                                   .where(UserCommandAccess.chat_id == chat_id,
                                          UserCommandAccess.telegram_id == telegram_id,
                                          UserCommandAccess.command == group))
        if row is not None:
            return bool(row.allowed)
    return None


async def set_personal_access(session: AsyncSession, chat_id: int, telegram_id: int, key: str,
                              allowed: bool | None) -> None:
    if allowed is None:
        await session.execute(sql_delete(UserCommandAccess)
                              .where(UserCommandAccess.chat_id == chat_id,
                                     UserCommandAccess.telegram_id == telegram_id,
                                     UserCommandAccess.command == key))
        return
    row = await session.get(UserCommandAccess, (chat_id, telegram_id, key))
    if row is None:
        session.add(UserCommandAccess(chat_id=chat_id, telegram_id=telegram_id, command=key,
                                      allowed=allowed))
    else:
        row.allowed = allowed
        row.updated_at = utcnow()


def key_group_of(key: str) -> str | None:
    """A leaf command belongs to the group its handlers declare (see the decorators)."""
    return COMMAND_GROUPS.get(key)


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
    """``(allowed, actor_level, required_level)`` for a command in a given chat.

    Уровень 6 выключает команду, уровень 0 открывает её всем, а личный доступ
    («+лдк»/«-лдк») важнее рангов: он и выдаёт исключение, и запрещает команду.
    """
    level = await staff_level(session, telegram_id)
    required = await command_min_level(session, chat_id, key)
    exception = await personal_access(session, chat_id, telegram_id, key)
    if exception is not None:
        return exception, level, required
    if required > 5:
        return False, level, required
    if required <= 0:
        return True, level, required
    return level >= required and level > 0, level, required


def describe_access(key: str, required: int, default: int) -> str:
    title = COMMANDS.get(key, (default, ""))[1]
    if required > 5:
        marker = " (выключено)"
        level_text = "выключено"
    elif required <= 0:
        marker = " (для всех)"
        level_text = "для всех"
    else:
        marker = "" if required == default else " (изменено)"
        level_text = f"от {required} уровня"
    return f"<code>{key}</code> — {level_text}{marker}: {title}"


def with_defaults(config: ChatConfig, **fields) -> ChatConfig:
    return replace(config, **fields)
