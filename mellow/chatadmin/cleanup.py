"""«Чистка чата»: mass deletion and kicking by activity.

Telegram allows a bot to delete up to 100 messages per call with an age limit of 48
hours, so older messages are reported honestly instead of being silently skipped. Every
destructive command asks for confirmation first and prints what it is about to do.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mellow.models import ChatMemberActivity, Punishment, utcnow
from mellow.moderation import warning_is_active

log = logging.getLogger("mellow.cleanup")

TELEGRAM_DELETE_LIMIT = 100
TELEGRAM_DELETE_AGE = timedelta(hours=48)
KICK_KINDS = {"кик неактив", "кик актив", "кик новичков", "кик удалённых"}
DEFAULT_INACTIVE_DAYS = 30
DEFAULT_ACTIVE_DAYS = 30
DEFAULT_NEWCOMER_DAYS = 1


@dataclass
class CleanupPlan:
    kind: str
    chat_id: int
    summary: str
    message_ids: list[int] = field(default_factory=list)
    targets: list[int] = field(default_factory=list)
    skipped_old: int = 0


def _cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


async def plan_message_cleanup(chat_id: int, message_ids: list[int]) -> CleanupPlan:
    """Plan a deletion: Telegram accepts at most 100 ids per call."""
    unique = list(dict.fromkeys(message_ids))
    actionable = [message_id for message_id in unique if message_id > 0][:TELEGRAM_DELETE_LIMIT]
    # Telegram message ids are increasing; anything older than 48 hours usually has a
    # much lower id, but the exact age is only known to Telegram, so the count of the
    # requested messages that cannot be deleted is reported instead of guessed.
    skipped = max(0, len(unique) - len(actionable))
    summary = f"Удаление {len(actionable)} сообщений" + (f" (пропущено {skipped})" if skipped else "")
    return CleanupPlan("удалить", chat_id, summary, message_ids=actionable, skipped_old=skipped)


async def plan_member_cleanup(session: AsyncSession, chat_id: int, kind: str, days: int | None) -> CleanupPlan:
    members = list((await session.scalars(select(ChatMemberActivity)
                                          .where(ChatMemberActivity.chat_id == chat_id,
                                                 ChatMemberActivity.is_member.is_(True)))).all())
    if kind == "кик неактив":
        cutoff = _cutoff(days or DEFAULT_INACTIVE_DAYS)
        targets = [m.telegram_id for m in members
                   if m.last_message_at is None or _aware(m.last_message_at) < cutoff]
        label = f"Кик неактива (не писали {(days or DEFAULT_INACTIVE_DAYS)} дн.)"
    elif kind == "кик актив":
        cutoff = _cutoff(days or DEFAULT_ACTIVE_DAYS)
        targets = [m.telegram_id for m in members
                   if m.last_message_at is not None and _aware(m.last_message_at) >= cutoff]
        label = f"Кик актива (писали за последние {(days or DEFAULT_ACTIVE_DAYS)} дн.)"
    elif kind == "кик новичков":
        cutoff = _cutoff(days or DEFAULT_NEWCOMER_DAYS)
        targets = [m.telegram_id for m in members
                   if m.joined_at is not None and _aware(m.joined_at) >= cutoff]
        label = f"Кик новичков (вошли за последние {(days or DEFAULT_NEWCOMER_DAYS)} дн.)"
    elif kind == "кик удалённых":
        targets = [m.telegram_id for m in members]
        label = "Проверка вышедших участников"
    else:
        raise ValueError(kind)
    return CleanupPlan(kind, chat_id, f"{label}: найдено {len(targets)}", targets=targets)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def delete_messages(bot: Bot, chat_id: int, message_ids: list[int]) -> tuple[int, int]:
    """Delete messages in chunks of 100. Returns ``(deleted, failed)``."""
    deleted = failed = 0
    for start in range(0, len(message_ids), TELEGRAM_DELETE_LIMIT):
        chunk = message_ids[start:start + TELEGRAM_DELETE_LIMIT]
        try:
            await bot.delete_messages(chat_id=chat_id, message_ids=chunk)
            deleted += len(chunk)
        except TelegramBadRequest as exc:
            # One undeletable (too old, already removed) message fails the whole batch, so
            # each id of a failed batch is retried alone to salvage the rest.
            for message_id in chunk:
                try:
                    await bot.delete_message(chat_id, message_id)
                    deleted += 1
                except (TelegramBadRequest, Exception) as inner:  # noqa: B014 - keep the loop alive
                    failed += 1
                    log.info("Message %s was not deleted: %s", message_id, type(inner).__name__)
            log.info("Batch deletion partially failed in chat %s: %s", chat_id, type(exc).__name__)
    return deleted, failed


async def purge_left_members(session: AsyncSession, chat_id: int, targets: list[int]) -> list[int]:
    """Mark members that left the chat and return their ids."""
    rows = list((await session.scalars(select(ChatMemberActivity)
                                        .where(ChatMemberActivity.chat_id == chat_id,
                                               ChatMemberActivity.telegram_id.in_(targets)))).all()) if targets else []
    for row in rows:
        row.is_member = False
        row.updated_at = utcnow()
    return [row.telegram_id for row in rows]


async def kick_members(bot: Bot, chat_id: int, targets: list[int]) -> tuple[int, int]:
    """Ban and immediately unban everyone in ``targets`` (a Telegram «кик»)."""
    kicked = failed = 0
    for target_id in targets:
        try:
            await bot.ban_chat_member(chat_id, target_id)
            await bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
            kicked += 1
        except Exception:
            failed += 1
    return kicked, failed


async def purge_inactive_punishments(session: AsyncSession) -> int:
    """Close expired mutes and warnings so lists stay honest."""
    now = utcnow()
    rows = list((await session.scalars(select(Punishment).where(Punishment.active.is_(True)))).all())
    closed = 0
    for row in rows:
        if row.type == "warn":
            if not warning_is_active(row, now):
                row.active = False
                closed += 1
        elif row.expires_at is not None and _aware(row.expires_at) <= now:
            row.active = False
            closed += 1
    return closed
