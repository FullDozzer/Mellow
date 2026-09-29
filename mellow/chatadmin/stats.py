"""«Статистическая информация»: чат, сообщения, вложения и профиль участника."""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mellow.chatadmin.config import ChatConfig
from mellow.config import Settings
from mellow.models import ChatMemberActivity, DailyMessageStat, MessageStat, Punishment, Trigger, User
from mellow.moderation import punishment_active
from mellow.stats import day_key, remaining_messages, render_chart
from mellow.statistics import days_label, format_moment


def series_days(days: int) -> list[str]:
    today = datetime.now(timezone.utc)
    return [day_key(today - timedelta(days=offset)) for offset in range(days - 1, -1, -1)]


async def daily_series(session: AsyncSession, chat_id: int, days: int) -> list[tuple[str, int]]:
    wanted = series_days(days)
    rows = (await session.execute(select(DailyMessageStat.day, DailyMessageStat.message_count)
                                  .where(DailyMessageStat.chat_id == chat_id,
                                         DailyMessageStat.day.in_(wanted)))).all()
    counts = {day: int(count) for day, count in rows}
    return [(day, counts.get(day, 0)) for day in wanted]


async def chat_totals(session: AsyncSession, chat_id: int) -> tuple[int, int]:
    messages, attachments = (await session.execute(
        select(func.coalesce(func.sum(DailyMessageStat.message_count), 0),
               func.coalesce(func.sum(DailyMessageStat.attachment_count), 0))
        .where(DailyMessageStat.chat_id == chat_id))).one()
    return int(messages), int(attachments)


async def member_totals(session: AsyncSession, chat_id: int) -> tuple[int, int]:
    present = await session.scalar(select(func.count()).select_from(ChatMemberActivity)
                                   .where(ChatMemberActivity.chat_id == chat_id,
                                          ChatMemberActivity.is_member.is_(True)))
    total = await session.scalar(select(func.count()).select_from(ChatMemberActivity)
                                 .where(ChatMemberActivity.chat_id == chat_id))
    return int(present or 0), int(total or 0)


async def punishment_counts(session: AsyncSession, chat_id: int) -> dict[str, int]:
    rows = list((await session.scalars(select(Punishment)
                                       .where(Punishment.chat_id == chat_id,
                                              Punishment.active.is_(True)))).all())
    counters = {"ban": 0, "mute": 0, "warn": 0, "kick": 0}
    for row in rows:
        if punishment_active(row):
            counters[row.type] = counters.get(row.type, 0) + 1
    return counters


async def chat_overview(session: AsyncSession, chat_id: int, config: ChatConfig,
                        grid_name: str | None = None) -> str:
    present, total_tracked = await member_totals(session, chat_id)
    messages, attachments = await chat_totals(session, chat_id)
    counters = await punishment_counts(session, chat_id)
    triggers_count = await session.scalar(select(func.count()).select_from(Trigger)
                                          .where(Trigger.chat_id == chat_id, Trigger.enabled.is_(True)))
    today = await session.scalar(select(DailyMessageStat.message_count)
                                 .where(DailyMessageStat.chat_id == chat_id, DailyMessageStat.day == day_key()))
    lines = [
        "<b>Чат инфо</b>",
        f"ID: <code>{chat_id}</code>",
        f"Участников в статистике: {present} из {total_tracked} отслеживаемых",
        f"Сообщений всего: {messages} (вложений: {attachments})",
        f"Сообщений сегодня: {int(today or 0)}",
        f"Наказания: банов {counters['ban']}, мутов {counters['mute']}, предупреждений {counters['warn']}",
        f"Триггеров настроено: {int(triggers_count or 0)}",
    ]
    if grid_name:
        lines.append(f"Сетка чатов: {html.escape(grid_name)}")
    filters = []
    if config.links_denied:
        filters.append("ссылки (" + ", ".join(config.denied_link_types or []) + ")")
    if config.sticker_limit is not None:
        filters.append(f"стикеры ≤ {config.sticker_limit}")
    if config.caps_percent:
        filters.append(f"капс ≥ {config.caps_percent}%")
    if config.voice_denied:
        filters.append("гс/кружки")
    if config.guest_bots_denied:
        filters.append("гостевые боты")
    if config.profanity_filter:
        filters.append("маты")
    lines.append("Фильтры: " + (", ".join(filters) if filters else "не включены"))
    lines.append(f"Лимит предупреждений: {config.warning_limit}; мут по умолчанию: "
                 f"{config.mute_default_seconds // 3600} ч., бан по умолчанию: "
                 + (f"{config.ban_default_seconds // 3600} ч." if config.ban_default_seconds
                    else "навсегда"))
    return "\n".join(lines)


async def chat_statistics(session: AsyncSession, chat_id: int, days: int,
                          config: ChatConfig) -> tuple[str, str | None]:
    """Returns ``(html, chart)``; the chart is None when it is disabled in the chat."""
    series = await daily_series(session, chat_id, days)
    total = sum(count for _, count in series)
    messages, _ = await chat_totals(session, chat_id)
    window_attachments = await _attachments(session, chat_id, days)
    counters = await punishment_counts(session, chat_id)
    busiest = max(series, key=lambda item: item[1]) if series else ("—", 0)
    lines = [
        f"<b>Статистика чата</b> за {days} {days_label(days)}",
        f"Сообщений: {total} (вложений: {window_attachments})",
        f"В среднем за день: {round(total / days, 1)}",
        f"Самый активный день: {busiest[0]} — {busiest[1]}",
        f"Всего за всё время: {messages}",
        f"Активные наказания: баны {counters['ban']}, муты {counters['mute']}, предупреждения {counters['warn']}",
    ]
    chart = None
    if config.show_charts:
        chart = render_chart(series, title=f"Сообщения по дням ({days} {days_label(days)})")
    return "\n".join(lines), chart


async def _attachments(session: AsyncSession, chat_id: int, days: int) -> int:
    wanted = series_days(days)
    total = await session.scalar(select(func.coalesce(func.sum(DailyMessageStat.attachment_count), 0))
                                 .where(DailyMessageStat.chat_id == chat_id,
                                        DailyMessageStat.day.in_(wanted)))
    return int(total or 0)


async def message_statistics(session: AsyncSession, settings: Settings) -> str:
    total = await session.scalar(select(func.coalesce(func.sum(MessageStat.message_count), 0)))
    tracked = await session.scalar(select(func.count()).select_from(MessageStat))
    reached = await session.scalar(select(func.count()).select_from(MessageStat)
                                   .where(MessageStat.threshold_reached.is_(True)))
    rows = (await session.execute(select(User.username, User.telegram_id, MessageStat.message_count)
                                  .join(User, MessageStat.user_id == User.id)
                                  .order_by(MessageStat.message_count.desc()).limit(10))).all()
    lines = ["<b>Статистика сообщений</b>",
             f"Всего в счёте: {int(total or 0)}",
             f"Участников в счёте: {int(tracked or 0)}",
             f"Выполнили порог ({settings.message_threshold}): {int(reached or 0)}",
             ""]
    for place, (username, telegram_id, count) in enumerate(rows, start=1):
        name = f"@{username}" if username else f"ID {telegram_id}"
        left = remaining_messages(settings, int(count))
        lines.append(f"{place}. {html.escape(name)} — {count}" + ("" if left == 0 else f", осталось {left}"))
    return "\n".join(lines)


async def user_statistics(session: AsyncSession, settings: Settings, telegram_id: int,
                          chat_id: int | None = None) -> str:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        return f"<b>Профиль</b> <code>{telegram_id}</code>\nДанных пока нет: пользователь не писал боту."
    stat = await session.get(MessageStat, user.id)
    count = int(stat.message_count) if stat else 0
    lines = [
        f"<b>Профиль</b> {html.escape('@' + user.username if user.username else str(telegram_id))}",
        f"Minecraft: {html.escape(user.minecraft_username) if user.minecraft_username else '—'}",
        f"Сообщений в счёте: {count}",
    ]
    if settings.message_requirement_enabled:
        left = remaining_messages(settings, count)
        lines.append(f"До порога ({settings.message_threshold}): " + ("порог выполнен ✅" if left == 0 else f"осталось {left}"))
    if stat and stat.first_message_at:
        lines.append(f"Первое сообщение: {format_moment(stat.first_message_at)}")
    if stat and stat.last_message_at:
        lines.append(f"Последнее сообщение: {format_moment(stat.last_message_at)}")
    if chat_id:
        member = await session.get(ChatMemberActivity, (chat_id, telegram_id))
        if member is not None:
            if member.joined_at:
                lines.append(f"В чате с: {format_moment(member.joined_at)}")
            lines.append("В чате: " + ("да" if member.is_member else "нет"))
    counter_names = {"ban": "бан", "mute": "мут", "warn": "предупреждение", "kick": "исключение"}
    rows = list((await session.scalars(select(Punishment)
                                       .where(Punishment.target_user_id == telegram_id,
                                              Punishment.active.is_(True))
                                       .order_by(Punishment.id.desc()).limit(10))).all())
    active = [row for row in rows if punishment_active(row)]
    if active:
        lines.append("<b>Активные наказания</b>")
        for row in active:
            expiry = f" до {format_moment(row.expires_at)}" if row.expires_at else ""
            lines.append(f"• {counter_names.get(row.type, row.type)}{expiry}"
                         + (f": {html.escape(row.reason)}" if row.reason else ""))
    else:
        lines.append("Активных наказаний нет.")
    return "\n".join(lines)
