"""Punishment rules shared by moderator commands, triggers and the chat guard.

One place decides how a punishment is stored, how it is applied in Telegram and how
the database is repaired when Telegram refuses (missing rights, user left the chat).
Warning limits and warning lifetime also live here, so a manual ``варн`` and an
automatic one behave identically.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import html

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatPermissions
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mellow.models import Punishment, utcnow
from mellow.services import audit

log = logging.getLogger("mellow.moderation")

PUNISHMENT_TYPES = {"бан": "ban", "кик": "kick", "мут": "mute", "варн": "warn"}
REMOVAL_TYPES = {"разбан": "ban", "размут": "mute", "снятьварн": "warn"}
PUNISHED_MARKERS = {"бан": "забанен", "кик": "исключён", "мут": "заглушен", "варн": "предупреждён"}

UNMUTE_PERMISSIONS = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True,
    can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True,
    can_send_other_messages=True, can_add_web_page_previews=True)

FOREVER_WORDS = {"навсегда", "перм", "пермач", "перманентно", "бессрочно", "вечно", "forever", "инф"}

_PERIOD_UNITS = {
    "с": 1, "сек": 1, "секунда": 1, "секунды": 1, "секунд": 1, "s": 1, "second": 1, "seconds": 1,
    "м": 60, "мин": 60, "минута": 60, "минуты": 60, "минут": 60, "m": 60, "min": 60, "minute": 60,
    "ч": 3600, "час": 3600, "часа": 3600, "часов": 3600, "h": 3600, "hour": 3600, "hours": 3600,
    "д": 86400, "дн": 86400, "день": 86400, "дня": 86400, "дней": 86400, "сутки": 86400, "суток": 86400,
    "н": 604800, "нед": 604800, "неделя": 604800, "недели": 604800, "недель": 604800, "w": 604800,
    "мес": 2592000, "месяц": 2592000, "месяца": 2592000, "месяцев": 2592000,
    "г": 31536000, "год": 31536000, "года": 31536000, "лет": 31536000, "y": 31536000,
}


def parse_period(raw: str) -> int | None:
    """Parse «30м», «2 часа», «1 день», «навсегда» into seconds (``None`` = forever)."""
    value = (raw or "").strip().lower().replace("ё", "е")
    if not value:
        return None
    if value in FOREVER_WORDS:
        return None
    match = re.fullmatch(r"(\d+)\s*([a-zа-я]*)", value)
    if not match:
        raise ValueError("Укажи срок, например 30 минут, 2 часа, 7 дней или навсегда.")
    amount = int(match.group(1))
    if amount <= 0:
        raise ValueError("Срок должен быть больше нуля.")
    unit = match.group(2)
    if not unit:
        return amount * 60
    if unit not in _PERIOD_UNITS:
        raise ValueError("Неизвестная единица времени. Примеры: 30 минут, 2 часа, 7 дней.")
    return amount * _PERIOD_UNITS[unit]


def describe_period(seconds: int | None) -> str:
    if seconds is None:
        return "навсегда"
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"{days} дн." if days > 1 else "1 день"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} ч."
    if seconds % 60 == 0:
        return f"{seconds // 60} мин."
    return f"{seconds} сек."


def punishment_active(record: Punishment, now: datetime | None = None) -> bool:
    """True while a punishment still applies (SQLite returns naive datetimes)."""
    if not record.active:
        return False
    if record.expires_at is None:
        return True
    expires = record.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > (now or utcnow())


def warning_is_active(punishment: Punishment, now: datetime | None = None) -> bool:
    return punishment.type == "warn" and punishment_active(punishment, now)


async def active_warnings(session: AsyncSession, target_id: int) -> list[Punishment]:
    rows = await session.scalars(select(Punishment)
                                 .where(Punishment.target_user_id == target_id, Punishment.type == "warn",
                                        Punishment.active.is_(True))
                                 .order_by(Punishment.id))
    return [row for row in rows.all() if warning_is_active(row)]


async def perform_telegram_action(bot: Bot, chat_id: int, action: str, target_id: int,
                                  duration: int | None = None) -> None:
    """Apply one action in Telegram; raises when Telegram refuses it."""
    until = datetime.now(timezone.utc) + timedelta(seconds=duration) if duration else None
    if action == "бан":
        await bot.ban_chat_member(chat_id, target_id, until_date=until)
    elif action == "разбан":
        await bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
    elif action == "кик":
        await bot.ban_chat_member(chat_id, target_id)
        await bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
    elif action == "мут":
        await bot.restrict_chat_member(chat_id, target_id, permissions=ChatPermissions(can_send_messages=False),
                                       until_date=until or (datetime.now(timezone.utc) + timedelta(days=7)))
    elif action == "размут":
        await bot.restrict_chat_member(chat_id, target_id, permissions=UNMUTE_PERMISSIONS)
    elif action in {"варн", "удалить"}:
        return
    else:
        raise ValueError(f"unsupported moderation action: {action}")


@dataclass(frozen=True)
class PunishmentResult:
    applied: bool
    punishment_id: int | None = None
    error: str | None = None
    banned_by_limit: bool = False


async def apply_punishment(bot: Bot, session_factory, *, chat_id: int, target_id: int, action: str,
                           duration: int | None = None, reason: str | None = None,
                           actor_id: int | None = None, enforce_limit: bool = True) -> PunishmentResult:
    """Record a punishment, apply it in Telegram and keep both sides consistent.

    ``actor_id`` stays ``None`` for automatic actions performed by triggers, matching
    the project rule that anonymous actors are never written to the audit log.
    """
    if action not in PUNISHMENT_TYPES:
        return PunishmentResult(False, error=f"unsupported action: {action}")
    async with session_factory() as session, session.begin():
        punishment = Punishment(chat_id=chat_id, target_user_id=target_id, moderator_id=actor_id,
                                type=PUNISHMENT_TYPES[action], reason=reason, duration=duration,
                                expires_at=(datetime.now(timezone.utc) + timedelta(seconds=duration)) if duration else None)
        session.add(punishment)
        await session.flush()
        punishment_id = punishment.id
        await audit(session, f"punishment_{PUNISHMENT_TYPES[action]}", actor_id, f"telegram:{target_id}",
                    {"reason": reason, "duration": duration, "chat_id": chat_id, "automatic": actor_id is None})
    try:
        await perform_telegram_action(bot, chat_id, action, target_id, duration)
    except (TelegramBadRequest, TelegramForbiddenError):
        async with session_factory() as session, session.begin():
            row = await session.get(Punishment, punishment_id)
            if row is not None:
                row.active = False
            await audit(session, "moderation_api_not_confirmed", actor_id, f"telegram:{target_id}",
                        {"command": action, "chat_id": chat_id})
        log.warning("Telegram did not confirm %s for a target in chat %s", action, chat_id)
        return PunishmentResult(False, punishment_id, "telegram")
    banned_by_limit = False
    if action == "варн" and enforce_limit:
        banned_by_limit = await enforce_warning_limit(bot, session_factory, chat_id, target_id, actor_id=actor_id)
    return PunishmentResult(True, punishment_id, banned_by_limit=banned_by_limit)


async def enforce_warning_limit(bot: Bot, session_factory, chat_id: int, target_id: int,
                                actor_id: int | None = None) -> bool:
    """Ban a member who reached the chat's warning limit. ``True`` when banned."""
    # Local imports avoid a cycle: the chat administration layer imports this module.
    from mellow.chatadmin.config import chat_config
    from mellow.chatadmin.triggers import trigger_actions

    async with session_factory() as session:
        config = await chat_config(session, chat_id)
        warnings = await active_warnings(session, target_id)
        if config.warning_limit <= 0 or len(warnings) < config.warning_limit:
            return False
        actions = await trigger_actions(session, chat_id, "варн лимит")
    if not actions:
        actions = [{"command": "бан", "duration": config.warning_ban_seconds, "reason": "Лимит предупреждений"}]
    for action in actions:
        await apply_punishment(bot, session_factory, chat_id=chat_id, target_id=target_id,
                               action=action["command"], duration=action.get("duration"),
                               reason=action.get("reason") or "Лимит предупреждений",
                               actor_id=actor_id, enforce_limit=False)
    return True


def cap_duration(settings, level: int, duration: int | None) -> int | None:
    """Limit a punishment to what the acting level may issue (``0`` = unlimited)."""
    entry = settings.levels.get(level) if settings is not None else None
    maximum = entry.max_punishment_seconds if entry is not None else 0
    if not maximum:
        return duration
    if duration is None:
        return maximum
    return min(duration, maximum)


def punishment_summary(record: Punishment) -> str:
    label = {"ban": "Бан", "mute": "Мут", "warn": "Предупреждение", "kick": "Исключение"}.get(record.type, record.type)
    reason = html.escape(record.reason) if record.reason else "без причины"
    expiry = f", до {record.expires_at:%d.%m.%Y %H:%M} UTC" if record.expires_at else ""
    return f"#{record.id} {label}: {reason}{expiry}"
