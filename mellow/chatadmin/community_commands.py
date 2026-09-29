"""Команды модерации: состав модерации, созыв, завещание и передача создателя.

These are the parts of the first two Iris sections that do not fit into the rank and
punishment files: who appointed whom, who is online, calling the staff, the creator's
will and the handover of the creator title.
"""

from __future__ import annotations

import html
from datetime import timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from mellow.chatadmin.context import ChatContext, command
from mellow.chatadmin.moderation_commands import rank_title, target_or_reply
from mellow.models import AuditLog, ChatMemberActivity, CreatorWill, Punishment, Staff, User, utcnow
from mellow.services import audit, staff_level
from mellow.statistics import format_moment

ONLINE_WINDOW = timedelta(hours=24)
SUMMON_LIMIT = 20
WILL_HEIR_LEVEL = 5


def online_marker(status: str, last_message_at) -> str:
    """🟢/⚪/➖ as in the documentation, adapted to what a bot may know.

    Telegram does not tell a bot when a person was last seen, so activity in the chat is
    the substitute: a message within the last day means online, membership without recent
    activity means offline, and not being in the chat at all is «не в чате».
    """
    if status in {"left", "kicked"}:
        return "➖"
    if last_message_at is None:
        return "⚪"
    moment = last_message_at if last_message_at.tzinfo else last_message_at.replace(tzinfo=utcnow().tzinfo)
    return "🟢" if utcnow() - moment <= ONLINE_WINDOW else "⚪"


async def _names(session, telegram_ids: list[int]) -> dict[int, User]:
    wanted = [int(value) for value in telegram_ids if value]
    if not wanted:
        return {}
    rows = await session.scalars(select(User).where(User.telegram_id.in_(wanted)))
    return {user.telegram_id: user for user in rows.all()}


def _username(user: User | None, telegram_id: int) -> str:
    if user is not None and user.username:
        return f"@{user.username}"
    return f"<code>{telegram_id}</code>"


# --------------------------------------------------------------------------------------
# Кто назначил, мой онлайн
# --------------------------------------------------------------------------------------

@command("кто назначил", key_group="модер")
@command("роль", key_group="модер")
async def cmd_appointed_by(ctx: ChatContext):
    target_id = await target_or_reply(ctx)
    if target_id is None:
        return
    async with ctx.session_factory() as session:
        rows = (await session.scalars(select(AuditLog)
                                      .where(AuditLog.action.in_(("staff_level_set", "staff_removed")),
                                             AuditLog.target_ref == f"telegram:{target_id}")
                                      .order_by(AuditLog.id.desc()).limit(5))).all()
        names = await _names(session, [target_id, *[int(row.actor_id) for row in rows if row.actor_id]])
        target_level = await staff_level(session, target_id)
    if not rows:
        await ctx.reply("В журнале нет назначений этого пользователя.")
        return
    lines = [f"<b>Ранг {html.escape(_username(names.get(target_id), target_id))}</b>: "
             f"{html.escape(rank_title(target_level, ctx.settings))}"]
    for row in rows:
        level = (row.details or {}).get("new_level")
        actor_id = int(row.actor_id) if row.actor_id else 0
        actor = html.escape(_username(names.get(actor_id), actor_id)) if actor_id else "бот"
        action = "снял ранг" if row.action == "staff_removed" else f"выдал {level} ранг"
        lines.append(f"• {format_moment(row.created_at)} — {actor} {action}")
    await ctx.reply("\n".join(lines))


@command("+мой онлайн", key_group="модер")
@command("-мой онлайн", key_group="модер")
@command("мой онлайн", key_group="модер", public=True)
async def cmd_my_online(ctx: ChatContext):
    enabled = not ctx.command.startswith("-")
    async with ctx.session_factory() as session, session.begin():
        user = await session.scalar(select(User).where(User.telegram_id == ctx.actor_id))
        if user is None:
            user = User(telegram_id=ctx.actor_id)
            session.add(user)
            await session.flush()
        staff = await session.get(Staff, user.id)
        if staff is None:
            await ctx.reply("Показывать статус может только модератор.")
            return
        if ctx.command == "мой онлайн":
            state = "включено" if staff.show_online else "выключено"
            await ctx.reply(f"Отображение твоего статуса в списке модерации: {state}.")
            return
        staff.show_online = enabled
        staff.updated_at = utcnow()
    await ctx.reply("Твой статус в списке модерации: " + ("включён." if enabled else "скрыт (⚪)."))


# --------------------------------------------------------------------------------------
# Созыв модерации
# --------------------------------------------------------------------------------------

@command("созвать модеров", key_group="модер")
@command("позвать модеров", key_group="модер")
@command("созвать админов", key_group="модер")
@command("позвать админов", key_group="модер")
async def cmd_summon_staff(ctx: ChatContext):
    async with ctx.session_factory() as session:
        rows = (await session.scalars(select(Staff).where(Staff.active.is_(True))
                                      .order_by(Staff.level.desc()))).all()
        by_id = {}
        if rows:
            for user in (await session.scalars(select(User).where(
                    User.id.in_([staff.user_id for staff in rows])))).all():
                by_id[user.id] = user
        activity = {row.telegram_id: row.last_message_at for row in
                    (await session.scalars(select(ChatMemberActivity)
                                           .where(ChatMemberActivity.chat_id == ctx.chat_id))).all()}
    members = [(staff, by_id[staff.user_id]) for staff in rows if staff.user_id in by_id]
    members = [(staff, user) for staff, user in members if user.telegram_id != ctx.actor_id]
    if not members:
        await ctx.reply("В составе модерации никого нет.")
        return
    # Больше двадцати человек — зовём тех, кто писал в чате последним.
    members.sort(key=lambda item: activity.get(item[1].telegram_id) or utcnow(), reverse=True)
    mentions = []
    for staff, user in members[:SUMMON_LIMIT]:
        label = (f"@{user.username}" if user.username
                 else f'<a href="tg://user?id={user.telegram_id}">модератор</a>')
        mentions.append(f"{label} ({staff.level})")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➖ Удалить упоминания", callback_data="summon:delete")]])
    await ctx.reply("<b>Созыв модерации</b>\n" + ", ".join(mentions), reply_markup=keyboard)


# --------------------------------------------------------------------------------------
# Завещание и передача создателя
# --------------------------------------------------------------------------------------

@command("+завещание", key_group="завещание")
@command("+наследство", key_group="завещание")
async def cmd_will_set(ctx: ChatContext):
    target_id = await target_or_reply(ctx, missing="Укажи наследника: <code>+завещание @ник</code>.")
    if target_id is None:
        return
    if target_id == ctx.actor_id:
        await ctx.reply("Наследником нельзя назначить себя.")
        return
    async with ctx.session_factory() as session, session.begin():
        row = await session.get(CreatorWill, ctx.actor_id)
        if row is None:
            session.add(CreatorWill(telegram_id=ctx.actor_id, heir_telegram_id=target_id))
        else:
            row.heir_telegram_id, row.updated_at = target_id, utcnow()
        await audit(session, "will_set", ctx.actor_id, f"telegram:{target_id}", {"chat_id": ctx.chat_id})
    await ctx.reply(f"Завещание оставлено на <code>{target_id}</code>. Если доступ к аккаунту будет утерян, "
                    "наследник вступит в права командой «вступить в наследство».")


@command("моё завещание", key_group="завещание", public=True)
@command("мое завещание", key_group="завещание", public=True)
async def cmd_will_show(ctx: ChatContext):
    async with ctx.session_factory() as session:
        row = await session.get(CreatorWill, ctx.actor_id)
        names = await _names(session, [row.heir_telegram_id]) if row else {}
    if row is None:
        await ctx.reply("Завещания нет: <code>+завещание @ник</code>.")
        return
    heir = html.escape(_username(names.get(row.heir_telegram_id), row.heir_telegram_id))
    await ctx.reply(f"Наследник: {heir}\nОставлено: {format_moment(row.created_at)}")


@command("-завещание", key_group="завещание")
@command("-наследство", key_group="завещание")
async def cmd_will_clear(ctx: ChatContext):
    async with ctx.session_factory() as session, session.begin():
        row = await session.get(CreatorWill, ctx.actor_id)
        if row is None:
            await ctx.reply("Завещания нет.")
            return
        await session.delete(row)
        await audit(session, "will_cleared", ctx.actor_id, f"telegram:{row.heir_telegram_id}",
                    {"chat_id": ctx.chat_id})
    await ctx.reply("Завещание аннулировано.")


async def _grant_level(session, telegram_id: int, level: int) -> None:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        user = User(telegram_id=telegram_id)
        session.add(user)
        await session.flush()
    staff = await session.get(Staff, user.id)
    if staff is None:
        session.add(Staff(user_id=user.id, level=level, active=True))
    else:
        staff.level, staff.active, staff.updated_at = level, True, utcnow()
    return user


@command("вступить в наследство", key_group="завещание", public=True)
async def cmd_will_inherit(ctx: ChatContext):
    target_id = await target_or_reply(ctx, missing="Укажи, от кого наследуешь: "
                                                   "<code>вступить в наследство @ник</code>.")
    if target_id is None:
        return
    async with ctx.session_factory() as session, session.begin():
        will = await session.get(CreatorWill, target_id)
        if will is None or will.heir_telegram_id != ctx.actor_id:
            await ctx.reply("Этот пользователь не оставлял тебе завещание.")
            return
        if await staff_level(session, ctx.actor_id) >= WILL_HEIR_LEVEL:
            await ctx.reply("У тебя уже есть права создателя.")
            return
        await _grant_level(session, ctx.actor_id, WILL_HEIR_LEVEL)
        await session.delete(will)
        await audit(session, "will_inherited", ctx.actor_id, f"telegram:{target_id}", {"chat_id": ctx.chat_id})
    await ctx.reply("Ты вступил в наследство и получил права создателя. Состав модерации: "
                    "<code>кто админ</code>.")


@command("передать создателя", key_group="завещание", public=True)
async def cmd_transfer_creator(ctx: ChatContext):
    target_id = await target_or_reply(ctx, missing="Укажи преемника: <code>передать создателя @ник</code>.")
    if target_id is None:
        return
    if target_id == ctx.actor_id:
        await ctx.reply("Нельзя передать права создателя самому себе.")
        return
    async with ctx.session_factory() as session, session.begin():
        if ctx.actor_level < WILL_HEIR_LEVEL:
            await ctx.reply("Передать права создателя может только создатель (5 ранг).")
            return
        if await staff_level(session, target_id) >= WILL_HEIR_LEVEL:
            await ctx.reply("У этого пользователя уже есть права создателя.")
            return
        await _grant_level(session, target_id, WILL_HEIR_LEVEL)
        # Прежний создатель остаётся администратором: вернуть себе права сам он не сможет.
        actor = await session.scalar(select(User).where(User.telegram_id == ctx.actor_id))
        if actor is not None:
            actor_staff = await session.get(Staff, actor.id)
            if actor_staff is not None:
                actor_staff.level = WILL_HEIR_LEVEL - 1
                actor_staff.updated_at = utcnow()
        await audit(session, "creator_transferred", ctx.actor_id, f"telegram:{target_id}",
                    {"chat_id": ctx.chat_id})
    await ctx.reply(f"Права создателя переданы <code>{target_id}</code>. Твой ранг понижен до "
                    f"{html.escape(rank_title(WILL_HEIR_LEVEL - 1, ctx.settings))}: вернуть себе права "
                    "самостоятельно ты больше не сможешь.")


# --------------------------------------------------------------------------------------
# Причина наказания, тихий кик, амнистия
# --------------------------------------------------------------------------------------

@command("причина", key_group="баны")
async def cmd_reason(ctx: ChatContext):
    target_id = await target_or_reply(ctx)
    if target_id is None:
        return
    async with ctx.session_factory() as session:
        row = await session.scalar(select(Punishment)
                                   .where(Punishment.target_user_id == target_id,
                                          Punishment.type.in_(("ban", "mute", "warn")))
                                   .order_by(Punishment.id.desc()))
        names = await _names(session, [row.moderator_id]) if row and row.moderator_id else {}
    if row is None:
        await ctx.reply("Наказаний у этого пользователя не найдено.")
        return
    titles = {"ban": "бан", "mute": "мут", "warn": "предупреждение"}
    moderator = (html.escape(_username(names.get(row.moderator_id), row.moderator_id))
                 if row.moderator_id else "бот")
    await ctx.reply(f"<b>Причина: {titles.get(row.type, row.type)}</b>\n"
                    f"Пользователь: <code>{target_id}</code>\n"
                    f"Модератор: {moderator}\n"
                    f"Дата: {format_moment(row.created_at)}\n"
                    f"Причина: {html.escape(row.reason or '—')}")


@command("кик тихо", key_group="кик")
async def cmd_quiet_kick(ctx: ChatContext):
    target_id = await target_or_reply(ctx)
    if target_id is None:
        return
    from mellow.moderation import apply_punishment
    result = await apply_punishment(ctx.bot, ctx.session_factory, chat_id=ctx.chat_id, target_id=target_id,
                                    action="кик", duration=None, reason=ctx.reason, actor_id=ctx.actor_id)
    try:
        await ctx.message.delete()
    except Exception:  # удаление своего сообщения — не повод падать
        pass
    if not result.applied:
        await ctx.reply(f"Не удалось исключить: {html.escape(result.error or 'нет прав')}.")


@command("амнистия", key_group="баны")
async def cmd_amnesty(ctx: ChatContext):
    async with ctx.session_factory() as session, session.begin():
        rows = (await session.scalars(select(Punishment)
                                      .where(Punishment.chat_id == ctx.chat_id,
                                             Punishment.type == "ban",
                                             Punishment.active.is_(True)))).all()
        for row in rows:
            row.active = False
        closed = len(rows)
        await audit(session, "amnesty", ctx.actor_id, f"chat:{ctx.chat_id}", {"closed": closed})
    if not closed:
        await ctx.reply("Активных банов нет.")
        return
    await ctx.reply(f"Амнистия: снято банов — {closed}.")


# --------------------------------------------------------------------------------------
# Служебное
# --------------------------------------------------------------------------------------

@command("смс ид", key_group="чистка", public=True)
async def cmd_message_id(ctx: ChatContext):
    if ctx.reply_target is None:
        await ctx.reply("Ответь этой командой на сообщение, чтобы узнать его ID.")
        return
    await ctx.reply(f"ID сообщения: <code>{ctx.reply_target.message_id}</code>")


@command("сетка снимаю полномочия", key_group="сетка")
@command("сетка ухожу в отставку", key_group="сетка")
async def cmd_grid_resign(ctx: ChatContext):
    from mellow.chatadmin.admin_commands import grid_resign
    from mellow.chatadmin.grid import grid_of_chat
    async with ctx.session_factory() as session:
        name = await grid_of_chat(session, ctx.chat_id)
    if name is None:
        await ctx.reply("Чат не привязан к сетке.")
        return
    await grid_resign(ctx, name)
