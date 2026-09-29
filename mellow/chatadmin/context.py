"""Shared state, parsing helpers and the command table for chat administration."""

from __future__ import annotations

import re
from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import Message
from sqlalchemy import select

from mellow.chatadmin.config import ChatSettingsStore
from mellow.config import Settings
from mellow.models import User

PREFIX_RE = re.compile(r"^\s*(?:[!./]\s*|(?:ирис|ириска)[,!]?\s+)", re.IGNORECASE)
GROUP_TYPES = {"group", "supergroup"}
TARGET_RE = re.compile(r"^(?:@\w{3,32}|\d{4,12}|https?://t\.me/\w+|t\.me/\w+)$")
FOREVER_WORDS = {"навсегда", "перм", "пермач", "перманентно", "бессрочно", "вечно"}


@dataclass
class ChatContext:
    """Everything a chat administration command needs."""

    bot: Bot
    message: Message
    settings: Settings
    session_factory: object
    store: ChatSettingsStore
    recent: object
    command: str
    args: list[str]
    tail: str
    actor_id: int
    chat_id: int
    actor_level: int = 0

    async def reply(self, text: str, **kwargs) -> None:
        kwargs.setdefault("parse_mode", "HTML")
        await self.message.reply(text, **kwargs)

    @property
    def reply_target(self) -> Message | None:
        return self.message.reply_to_message

    @property
    def reason(self) -> str | None:
        """Reason from the same line («Варн @user Флуд») or from the next line."""
        if self.tail:
            return self.tail[:500]
        inline = " ".join(self.args)
        return inline[:500] or None


def normalize_text(raw: str) -> str:
    return " ".join((raw or "").lower().replace("ё", "е").split())


def strip_prefix(text: str) -> str:
    return PREFIX_RE.sub("", text, count=1).strip()


class CommandTable:
    """Longest-match keyword table: «снять все варны» wins over «снять»."""

    def __init__(self):
        self._handlers: dict[str, tuple] = {}
        self._keys_by_size: dict[int, set[str]] = {}

    def add(self, key: str, handler, *, key_group: str | None = None, public: bool = False) -> None:
        normalized = normalize_text(key)
        self._handlers[normalized] = (handler, key_group, public)
        self._keys_by_size.setdefault(len(normalized.split()), set()).add(normalized)
        if key_group:
            from mellow.chatadmin.config import COMMAND_GROUPS
            COMMAND_GROUPS.setdefault(normalized, key_group)

    def resolve(self, text: str) -> tuple[str | None, list[str]]:
        words = text.split()
        for size in sorted(self._keys_by_size, reverse=True):
            if size > len(words):
                continue
            candidate = normalize_text(" ".join(words[:size]))
            if candidate in self._handlers:
                return candidate, words[size:]
        return None, []

    def handler_for(self, key: str):
        return self._handlers.get(key)

    def keys(self) -> list[str]:
        return sorted(self._handlers)


TABLE = CommandTable()


def command(key: str, *, key_group: str | None = None, public: bool = False):
    """Register a handler for a keyword; ``key_group`` is its «Доступ команд» section."""

    def decorator(func):
        TABLE.add(key, func, key_group=key_group, public=public)
        if key_group:
            from mellow.chatadmin.config import COMMAND_GROUPS
            COMMAND_GROUPS.setdefault(key, key_group)
        return func
    return decorator


def extract_target(tokens: list[str]) -> tuple[str | None, list[str]]:
    """Pull the first token that looks like a user reference out of the arguments."""
    for index, token in enumerate(tokens):
        if TARGET_RE.match(token):
            return token, tokens[:index] + tokens[index + 1:]
    return None, list(tokens)


def looks_like_period(token: str) -> bool:
    lowered = token.lower().strip(",")
    if lowered in FOREVER_WORDS:
        return True
    return bool(re.fullmatch(r"\d+\s*[a-zа-я]*", lowered))


def extract_period(tokens: list[str]) -> tuple[str | None, list[str]]:
    for index, token in enumerate(tokens):
        if looks_like_period(token):
            return token, tokens[:index] + tokens[index + 1:]
    return None, list(tokens)


def parse_leading_period(tokens: list[str]) -> tuple[int | None, str | None, int]:
    """Parse «30 минут», «2ч», «7 дней» from the first tokens.

    Multi-word periods are tried first, so «бан 7 дней» bans for a week instead of
    seven minutes. Returns ``(seconds, matched_text, consumed_tokens)``.
    """
    from mellow.moderation import parse_period

    if not tokens or not looks_like_period(tokens[0]):
        return None, None, 0
    for size in (3, 2, 1):
        if len(tokens) < size:
            continue
        candidate = " ".join(tokens[:size])
        try:
            seconds = parse_period(candidate)
        except ValueError:
            continue
        return seconds, candidate, size
    return None, None, 0


async def resolve_user_id(session_factory, reference: str | None, reply_to: Message | None) -> int | None:
    """Resolve «@username», a numeric id or a replied message into a Telegram id."""
    if reply_to is not None and reply_to.sender_chat is None and reply_to.from_user is not None:
        return reply_to.from_user.id
    if not reference:
        return None
    value = reference.strip()
    if value.startswith("http://") or value.startswith("https://"):
        value = value.split("t.me/", 1)[-1]
    value = value.lstrip("@")
    if value.isdigit():
        return int(value)
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.username.ilike(value)))
    return user.telegram_id if user else None
