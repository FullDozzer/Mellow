"""Message-count statistics for the community chat.

The counter itself lives in :mod:`mellow.middleware`; this module only reads it and
renders human answers to the questions members actually ask: "how many messages did
I write" and "how many are left before the threshold". Administrators are excluded
from the counter by design, so they are reported separately.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mellow.config import Settings
from mellow.models import MessageStat, User


@dataclass(frozen=True)
class MemberProgress:
    label: str
    message_count: int
    remaining: int

    @property
    def threshold_reached(self) -> bool:
        return self.remaining <= 0


@dataclass(frozen=True)
class CommunityStatistics:
    total_messages: int
    tracked_members: int
    threshold_reached: int
    top: list[MemberProgress] = field(default_factory=list)


def remaining_messages(settings: Settings, message_count: int) -> int:
    return max(0, settings.message_threshold - message_count)


def _label(username: str | None, telegram_id: int) -> str:
    return f"@{username}" if username else f"ID {telegram_id}"


async def member_progress(session: AsyncSession, settings: Settings, telegram_id: int) -> MemberProgress:
    count = await session.scalar(select(MessageStat.message_count)
                                 .join(User, MessageStat.user_id == User.id)
                                 .where(User.telegram_id == telegram_id))
    message_count = int(count or 0)
    return MemberProgress(label="", message_count=message_count,
                          remaining=remaining_messages(settings, message_count))


async def community_statistics(session: AsyncSession, settings: Settings, limit: int = 20) -> CommunityStatistics:
    total = await session.scalar(select(func.coalesce(func.sum(MessageStat.message_count), 0)))
    tracked = await session.scalar(select(func.count()).select_from(MessageStat))
    reached = await session.scalar(select(func.count()).select_from(MessageStat)
                                   .where(MessageStat.threshold_reached.is_(True)))
    rows = (await session.execute(select(User.username, User.telegram_id, MessageStat.message_count)
                                  .join(User, MessageStat.user_id == User.id)
                                  .order_by(MessageStat.message_count.desc(), MessageStat.user_id.asc())
                                  .limit(max(1, limit)))).all()
    top = [MemberProgress(label=_label(username, telegram_id), message_count=int(count),
                          remaining=remaining_messages(settings, int(count)))
           for username, telegram_id, count in rows]
    return CommunityStatistics(total_messages=int(total or 0), tracked_members=int(tracked or 0),
                               threshold_reached=int(reached or 0), top=top)


def _disabled_notice() -> str:
    return ("📊 <b>Статистика</b>\n\n"
            "Сбор статистики сообщений отключён администрацией, поэтому прогресс по порогу не считается.")


def render_member_progress(progress: MemberProgress, settings: Settings, *, title: str = "📊 Твоя статистика") -> str:
    if not settings.message_requirement_enabled:
        return _disabled_notice()
    lines = [f"<b>{html.escape(title)}</b>", "",
             f"Сообщений в чате: <b>{progress.message_count}</b>",
             f"Порог для whitelist: <b>{settings.message_threshold}</b>"]
    if progress.threshold_reached:
        lines.append("Порог выполнен ✅")
    else:
        lines.append(f"Осталось написать: <b>{progress.remaining}</b>")
    return "\n".join(lines)


def render_community_statistics(stats: CommunityStatistics, settings: Settings, *,
                                title: str = "📊 Статистика чата") -> str:
    if not settings.message_requirement_enabled:
        return _disabled_notice()
    lines = [f"<b>{html.escape(title)}</b>", "",
             f"Всего сообщений: <b>{stats.total_messages}</b>",
             f"Участников в счёте: <b>{stats.tracked_members}</b>",
             f"Порог ({settings.message_threshold}) выполнили: <b>{stats.threshold_reached}</b>",
             "",
             "<b>Прогресс участников</b>"]
    if not stats.top:
        lines.append("Пока в зачёт не попало ни одного сообщения.")
    for place, member in enumerate(stats.top, start=1):
        name = html.escape(member.label)
        if member.threshold_reached:
            lines.append(f"{place}. {name} — {member.message_count} ✅ порог выполнен")
        else:
            lines.append(f"{place}. {name} — {member.message_count}, осталось {member.remaining}")
    if stats.tracked_members > len(stats.top):
        lines.append(f"…и ещё участников: {stats.tracked_members - len(stats.top)}")
    lines.append("")
    lines.append("Администраторы в счётчик не попадают.")
    return "\n".join(lines)
