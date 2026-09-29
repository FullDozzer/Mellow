"""Content filters and the chat guard middleware.

The guard inspects group messages to answer one question — "does this violate the chat
rules" — and then deletes the offending message and applies the configured punishment.
Nothing is stored: the moderation log keeps the action, never the text.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque
from functools import lru_cache
from pathlib import Path

import yaml
from aiogram import BaseMiddleware
from aiogram.types import Message
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from mellow.chatadmin.config import ChatSettingsStore
from mellow.chatadmin.triggers import DEFAULT_ACTIONS, trigger_actions, trigger_level
from mellow.config import Settings
from mellow.models import ChatMemberActivity, utcnow
from mellow.moderation import apply_punishment, cap_duration
from mellow.services import staff_level

log = logging.getLogger("mellow.guard")

URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'()]+|(?<![\w.])t\.me/[^\s<>\"'()]+", re.IGNORECASE)
INVITE_RE = re.compile(r"t\.me/(?:\+|joinchat/)", re.IGNORECASE)
LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)

FILTER_EVENTS = ("маты", "ссылки", "стикеры", "голосовые", "гостевые боты", "капс")

# Common obfuscations of Russian profanity; kept tiny on purpose and extendable by file.
# Stems of four letters and longer on purpose: shorter ones match innocent Russian words
# («ебу» sits inside «требует», «манда» inside «мандарин»).
_DEFAULT_PROFANITY = ["хуй", "хуе", "пизд", "ебал", "ебан", "ебат", "ебусь", "блят", "бляд", "сука",
                      "сучк", "мудак", "мудил", "залуп", "пидор", "пидар", "гандон", "долбоеб",
                      "охуе", "нахуй", "похуй", "ахуе", "ублюд", "шлюх", "гнида", "debil", "fuck", "bitch"]
_MASKED_LETTER = "\x00"
# Only these characters are treated as a star hiding a letter: any other symbol (a dot,
# a comma, a space, a digit) is too common in ordinary text to be a disguise.
MASK_CHARS = "*#@!?_~^"
_LEET = str.maketrans({"0": "о", "1": "и", "3": "е", "4": "а", "$": "с", "@": "а", "6": "б", "9": "д",
                        "ё": "е"})
_JUNK_RE = re.compile(r"[^a-zа-я0-9]", re.IGNORECASE)
# A stem this short is only looked for as it is written; see contains_profanity().
SHORT_STEM_LENGTH = 4


@lru_cache(maxsize=1)
def profanity_stems() -> tuple[str, ...]:
    path = Path("config/profanity.yml")
    stems = _DEFAULT_PROFANITY
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict) and data.get("stems"):
            stems = [str(item).lower() for item in data["stems"]]
    except FileNotFoundError:
        pass
    except Exception:  # a broken file must not disable the filter silently
        log.warning("config/profanity.yml could not be read, using the built-in list")
    return tuple(stems)


def normalize_text(text: str) -> str:
    """Lowercase, undo leet substitutions and drop every separator.

    «Б л я т ь» and «бля-ть» both become «блять», which is what the stem list is checked
    against; a letter replaced by a star survives only in the raw text, see below.
    """
    lowered = text.lower().translate(_LEET)
    return _JUNK_RE.sub("", lowered)


def _variant_pattern(variant: str) -> str:
    parts = []
    for char in variant:
        if char == _MASKED_LETTER:
            parts.append(rf"[{re.escape(MASK_CHARS)}]")
        else:
            parts.append(rf"{re.escape(char)}+")
    return "".join(parts)


MAX_MASKED_LETTERS = 2


def _masked_variants(stem: str) -> list[str]:
    """All ways to hide letters of a stem behind stars.

    One star is only allowed in the middle of the word, two stars may reach its last
    letter as well («бл**ь»): a star at the very end of a single-star variant would match
    ordinary words followed by punctuation («сук »), which is not acceptable.
    """
    middle = list(range(1, len(stem) - 1))
    variants = [stem[:index] + _MASKED_LETTER + stem[index + 1:] for index in middle]
    positions = list(range(1, len(stem)))
    for first in range(len(positions)):
        for second in range(first + 1, len(positions)):
            left, right = positions[first], positions[second]
            variant = list(stem)
            variant[left] = variant[right] = _MASKED_LETTER
            variants.append("".join(variant))
    return variants


def _stem_pattern(stem: str) -> re.Pattern:
    """Match a stem in the raw text, allowing «бляяять» and starred letters («бл*ть», «бл**ь»).

    A letter may be doubled, and up to two letters inside the word may be replaced by a
    single non-letter character each — the usual way a word is disguised with stars.
    Interior positions only, so that «ебал» cannot be matched by «еб » with a space at the
    end, and at most two stars, so that ordinary punctuation cannot fake a whole word.
    """
    variants = _masked_variants(stem)
    variants.append(stem)
    return re.compile("|".join(_variant_pattern(variant) for variant in variants), re.IGNORECASE)


@lru_cache(maxsize=1)
def profanity_patterns() -> tuple[re.Pattern, ...]:
    return tuple(_stem_pattern(stem) for stem in profanity_stems())


def _word_is_profane(word: str) -> bool:
    """A single word, separators, case and leet substitutions ignored.

    Only stems of four letters and more are looked for here, because a short stem can sit
    inside an ordinary word («ебу» inside «требует»).
    """
    return any(len(stem) >= SHORT_STEM_LENGTH and stem in word for stem in profanity_stems())


def contains_profanity(text: str) -> bool:
    """Two passes, because «бл*ть» and «б л я т ь» need to be caught differently.

    The first pass walks over the words: every word is cut at punctuation and checked
    against the stem list, letters written one by one («б л я т ь») and a word broken by
    a hyphen or a dot («бля-ть») are glued back together. The second pass matches the raw
    text against the starred patterns, which is what catches a letter replaced by «*».
    """
    buffer = ""
    for token in text.split():
        parts = [part for part in _JUNK_RE.split(token.lower().translate(_LEET)) if part]
        if not parts:
            continue
        if all(len(part) <= 2 for part in parts):
            buffer += "".join(parts)
            if _word_is_profane(buffer):
                return True
            continue
        if any(_word_is_profane(part) for part in parts):
            return True
        if len(parts) > 1 and all(len(part) <= 3 for part in parts) and _word_is_profane("".join(parts)):
            return True
        buffer = ""
    lowered = text.lower().translate(_LEET)
    return any(pattern.search(lowered) for pattern in profanity_patterns())


def link_kind(url: str) -> str:
    """Telegram invites are «чаты», other t.me resources are «теги», the rest «сайты»."""
    lowered = url.lower()
    if INVITE_RE.search(lowered):
        return "чаты"
    if "t.me/" in lowered or lowered.startswith("tg://"):
        return "теги"
    return "сайты"


def link_is_allowed(url: str, allowed: list[str]) -> bool:
    candidate = url.lower().rstrip("/")
    for entry in allowed or []:
        normalized = str(entry).lower().strip().rstrip("/")
        if normalized and (candidate == normalized or normalized in candidate
                           or candidate.replace("https://", "").replace("http://", "") == normalized):
            return True
    return False


def find_links(text: str) -> list[tuple[str, str]]:
    return [(match.group(0), link_kind(match.group(0))) for match in URL_RE.finditer(text or "")]


def caps_ratio(text: str) -> tuple[int, int]:
    letters = [char for char in text if LETTER_RE.match(char)]
    if not letters:
        return 0, 0
    uppercase = sum(1 for char in letters if char.isupper())
    return round(uppercase * 100 / len(letters)), len(letters)


class RecentMessages:
    """Bounded memory of recent message ids per chat, for «Чистка чата».

    Only identifiers are kept; message text never is. The cache is intentionally small
    and in-memory: it exists to make «удалить 20» and «чистка смс» convenient, not to be
    a second archive of the chat.
    """

    def __init__(self, per_chat: int = 300):
        self._per_chat = per_chat
        self._messages: dict[int, deque[tuple[int, int]]] = defaultdict(lambda: deque(maxlen=per_chat))
        self._stickers: dict[tuple[int, int], int] = {}

    def remember(self, chat_id: int, message_id: int, user_id: int) -> None:
        self._messages[chat_id].append((message_id, user_id))

    def last(self, chat_id: int, count: int, user_id: int | None = None) -> list[int]:
        items = [item for item in self._messages.get(chat_id, ()) if user_id is None or item[1] == user_id]
        return [message_id for message_id, _ in items[-count:]]

    def count_sticker_streak(self, chat_id: int, user_id: int) -> int:
        key = (chat_id, user_id)
        self._stickers[key] = self._stickers.get(key, 0) + 1
        return self._stickers[key]

    def reset_sticker_streak(self, chat_id: int, user_id: int) -> None:
        self._stickers.pop((chat_id, user_id), None)


class ChatGuard(BaseMiddleware):
    """Applies the chat's filters to every group message before handlers run."""

    def __init__(self, settings: Settings, session_factory: async_sessionmaker,
                 store: ChatSettingsStore, recent: RecentMessages | None = None):
        self.settings = settings
        self.session_factory = session_factory
        self.store = store
        self.recent = recent or RecentMessages()
        self._staff_cache: dict[int, tuple[float, int]] = {}
        self._activity_cache: dict[tuple[int, int], float] = {}

    async def __call__(self, handler, event, data: dict):
        # Registered on updates, but tolerant: a message-level middleware passes the
        # Message itself as the event.
        message = event.message if hasattr(event, "message") else event
        bot = data.get("bot")
        if (bot is not None and isinstance(message, Message) and message.sender_chat is None
                and message.from_user is not None and not message.from_user.is_bot
                and message.chat.type in {"group", "supergroup"}):
            self.recent.remember(message.chat.id, message.message_id, message.from_user.id)
            try:
                await self.inspect(message, bot)
            except Exception:
                log.exception("Chat guard could not inspect a message")
        return await handler(event, data)

    async def _staff_level_cached(self, telegram_id: int) -> int:
        cached = self._staff_cache.get(telegram_id)
        now = time.monotonic()
        if cached is not None and now - cached[0] < 30:
            return cached[1]
        async with self.session_factory() as session:
            level = await staff_level(session, telegram_id)
        self._staff_cache[telegram_id] = (now, level)
        return level

    async def _touch_activity(self, chat_id: int, telegram_id: int) -> None:
        """Track the last message time, writing at most once a minute per member."""
        key = (chat_id, telegram_id)
        now = time.monotonic()
        if now - self._activity_cache.get(key, 0) < 60:
            return
        self._activity_cache[key] = now
        async with self.session_factory() as session, session.begin():
            result = await session.execute(update(ChatMemberActivity)
                                           .where(ChatMemberActivity.chat_id == chat_id,
                                                  ChatMemberActivity.telegram_id == telegram_id)
                                           .values(last_message_at=utcnow(), is_member=True))
            if not result.rowcount:
                session.add(ChatMemberActivity(chat_id=chat_id, telegram_id=telegram_id,
                                               joined_at=utcnow(), last_message_at=utcnow(), is_member=True))

    async def inspect(self, message, bot) -> None:
        config = await self.store.get(message.chat.id)
        level = await self._staff_level_cached(message.from_user.id)
        await self._touch_activity(message.chat.id, message.from_user.id)
        if level > 0:
            # Moderators are not filtered: their messages are not violations.
            self.recent.reset_sticker_streak(message.chat.id, message.from_user.id)
            return

        event = self._violation(message, config)
        if event is None:
            return
        if message.sticker is not None:
            limit = config.sticker_limit or 1
            if self.recent.count_sticker_streak(message.chat.id, message.from_user.id) < limit:
                return
        self.recent.reset_sticker_streak(message.chat.id, message.from_user.id)
        if event != "гостевые боты":
            # The offending message always goes away; a guest bot message is deleted too
            # (the guard also removes the bot's own message when Telegram allows it).
            try:
                await bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                log.info("Could not delete a filtered message in chat %s", message.chat.id)
        await self.apply_actions(message.chat.id, message.from_user.id, event, config, bot)

    def _violation(self, message, config) -> str | None:
        text = message.text or message.caption or ""
        if config.profanity_filter and text and contains_profanity(text):
            return "маты"
        if config.links_denied and text:
            for url, kind in find_links(text):
                if kind in (config.denied_link_types or []) and not link_is_allowed(url, config.allowed_links):
                    return "ссылки"
        if config.sticker_limit is not None and message.sticker is not None:
            return "стикеры"
        if config.voice_denied and (message.voice is not None or message.video_note is not None):
            return "голосовые"
        if config.guest_bots_denied and message.via_bot is not None:
            return "гостевые боты"
        if config.caps_percent and text:
            percent, letters = caps_ratio(text)
            if letters >= max(1, config.caps_min_length) and percent >= config.caps_percent:
                return "капс"
        return None

    async def apply_actions(self, chat_id: int, target_id: int, event: str, config, bot) -> None:
        async with self.session_factory() as session:
            actions = await trigger_actions(session, chat_id, event)
            level = await trigger_level(session, chat_id, event)
        if actions is None:
            actions = DEFAULT_ACTIONS.get(event)
        if not actions:
            return
        for action in actions:
            if action.get("command") == "удалить":
                continue
            duration = cap_duration(self.settings, level, action.get("duration"))
            await apply_punishment(bot, self.session_factory, chat_id=chat_id, target_id=target_id,
                                   action=action["command"], duration=duration,
                                   reason=action.get("reason") or DEFAULT_REASON.get(event),
                                   actor_id=None)


DEFAULT_REASON = {
    "маты": "Сквернословие",
    "ссылки": "Ссылки в чате",
    "стикеры": "Слишком много стикеров",
    "голосовые": "Голосовые сообщения запрещены",
    "гостевые боты": "Гостевой бот в чате",
    "капс": "КАПС в сообщении",
}
