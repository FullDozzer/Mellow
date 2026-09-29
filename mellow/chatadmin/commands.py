"""Command dispatch for chat administration.

A moderator types «мут 30 минут @user», «+триггер ссылки 1», «кик неактив 30 дней» — the
prefixed, human spelling of Iris rather than a slash-command. This module turns that text
into a command key plus raw arguments and calls the matching handler, checking the rank
required by «Доступ команд» first. The handlers themselves live in
:mod:`mellow.chatadmin.moderation_commands` and :mod:`mellow.chatadmin.admin_commands`.
"""

from __future__ import annotations

import html
import logging

from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message
from sqlalchemy import func, select

from mellow.chatadmin.admin_commands import *  # noqa: F401,F403 - registers handlers
from mellow.chatadmin.community_commands import *  # noqa: F401,F403 - registers handlers
from mellow.chatadmin.config import ChatSettingsStore, may_use
from mellow.chatadmin.context import GROUP_TYPES, TABLE, ChatContext, strip_prefix
from mellow.chatadmin.moderation_commands import *  # noqa: F401,F403 - registers handlers
from mellow.chatadmin.grid_commands import *  # noqa: F401,F403 - registers handlers
from mellow.chatadmin.profile_commands import *  # noqa: F401,F403 - registers handlers
from mellow.config import Settings
from mellow.services import staff_level

log = logging.getLogger("mellow.chatadmin")
router = Router(name="mellow-chatadmin")


async def handle_chat_command(message: Message, settings: Settings, session_factory,
                              store: ChatSettingsStore, recent, bot: Bot) -> bool:
    """Dispatch one group message. Returns ``True`` when it was an administration command."""
    if message.sender_chat is not None or message.from_user is None or not message.text:
        return False
    if message.chat.type not in GROUP_TYPES or message.from_user.is_bot:
        return False
    text = strip_prefix(message.text.split("\n", 1)[0])
    if not text or text.startswith("/"):
        return False
    key, args = TABLE.resolve(text)
    if key is None:
        return False
    handler, key_group, public = TABLE.handler_for(key)
    async with session_factory() as session:
        level = await staff_level(session, message.from_user.id)
        if not public:
            allowed, level, required = await may_use(session, message.chat.id, message.from_user.id, key)
        else:
            allowed, required = True, 0
    if not allowed:
        config = await store.get(message.chat.id)
        if config.notify_command_access:
            if required > 5:
                await message.reply("Эта команда выключена в чате.", parse_mode="HTML")
            elif level > 0:
                await message.reply(f"Недостаточно прав: команда «{html.escape(key)}» доступна с "
                                    f"{required} уровня (сейчас у тебя {level}).", parse_mode="HTML")
        return True
    try:
        await store.remember_title(message.chat.id, message.chat.title)
    except Exception:  # the title is cosmetic and must never block a command
        log.debug("Could not remember a chat title", exc_info=True)
    ctx = ChatContext(bot=bot, message=message, settings=settings, session_factory=session_factory,
                      store=store, recent=recent, command=key, args=args,
                      tail=message.text.split("\n", 1)[1].strip() if "\n" in message.text else "",
                      actor_id=message.from_user.id, chat_id=message.chat.id, actor_level=level)
    try:
        await handler(ctx)
    except ValueError as exc:
        await ctx.reply(html.escape(str(exc)))
    except Exception:
        log.exception("Chat command «%s» failed", key)
        await ctx.reply("Не удалось выполнить команду. Проверь права бота и попробуй ещё раз.")
    return True


@router.message(F.text)
async def chat_commands(message: Message, settings: Settings, session_factory, store: ChatSettingsStore,
                        recent, bot: Bot):
    await handle_chat_command(message, settings, session_factory, store, recent, bot)


# --------------------------------------------------------------------------------------
# Подтверждение чистки, вход и выход участников
# --------------------------------------------------------------------------------------

@router.callback_query(F.data == "summon:delete")
async def summon_callback(callback: CallbackQuery, session_factory, store: ChatSettingsStore):
    """«➖ Удалить упоминания» у созыва модерации: убирает сообщение с упоминаниями."""
    from mellow.services import staff_level
    async with session_factory() as session:
        level = await staff_level(session, callback.from_user.id)
    if level <= 0:
        await callback.answer("Созывать и удалять упоминания может только модерация.", show_alert=True)
        return
    try:
        await callback.message.delete()
    except Exception:
        await callback.answer("Не удалось удалить сообщение.", show_alert=True)
        return
    await callback.answer("Упоминания удалены")


@router.callback_query(F.data.startswith("cleanup:"))
async def cleanup_callback(callback: CallbackQuery, settings: Settings, session_factory,
                           store: ChatSettingsStore, bot: Bot):
    from mellow.chatadmin.admin_commands import run_cleanup, take_cleanup
    from mellow.chatadmin.config import may_use

    action, _, token = callback.data.split(":", 2)
    async with session_factory() as session:
        allowed, level, required = await may_use(session, callback.message.chat.id, callback.from_user.id, "чистка")
    if not allowed:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    pending = take_cleanup(token, callback.from_user.id)
    if pending is None:
        await callback.answer("Подтверждение устарело. Запусти команду заново.", show_alert=True)
        return
    if action.endswith("no"):
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Отменено")
        return
    await callback.answer("Выполняю…")
    summary = await run_cleanup(bot, pending, session_factory)
    await callback.message.edit_text(summary, reply_markup=None)


async def _track_members(message, session_factory, *, joined: bool,
                         invited_by: int | None = None) -> None:
    from mellow.chatadmin.admin_commands import _remember_members

    users = message.new_chat_members if joined else [message.left_chat_member]
    ids = [user.id for user in (users or []) if user is not None and not user.is_bot]
    if not ids:
        return
    async with session_factory() as session, session.begin():
        await _remember_members(session, message.chat.id, ids, joined, invited_by=invited_by)


async def _inviter_of(message) -> int | None:
    """Кто пригласил: в служебном сообщении это ``from_user``, если это не сам вошедший."""
    author = message.from_user
    if author is None or author.is_bot:
        return None
    joiners = {user.id for user in (message.new_chat_members or [])}
    if author.id in joiners:
        return None
    return author.id


async def _enforce_invite_policy(chat, joiners: list[int], inviter_id: int | None, config,
                                  session_factory, bot) -> None:
    """«Инвайты» и «Антирейд»: наказывается приглашающий, а не вошедший."""
    from datetime import timedelta

    from mellow.models import ChatMemberActivity, Punishment, utcnow
    from mellow.moderation import apply_punishment

    if inviter_id is None or not joiners:
        return
    if config.invite_limit:
        cutoff = utcnow() - timedelta(minutes=1)
        async with session_factory() as session:
            invitations = await session.scalar(
                select(func.count()).select_from(ChatMemberActivity)
                .where(ChatMemberActivity.chat_id == chat.id,
                       ChatMemberActivity.invited_by == inviter_id,
                       ChatMemberActivity.joined_at >= cutoff))
        if int(invitations or 0) > config.invite_limit:
            try:
                await bot.ban_chat_member(chat.id, inviter_id)
                await bot.unban_chat_member(chat.id, inviter_id, only_if_banned=True)
                await bot.send_message(chat.id, f"⚠️ <code>{inviter_id}</code> пригласил больше "
                                                f"{config.invite_limit} человек за раз и исключён.",
                                       parse_mode="HTML")
            except Exception:
                log.info("Could not kick an over-inviting member in chat %s", chat.id)
    if config.antiraid_limit:
        async with session_factory() as session:
            attempts = await session.scalar(
                select(func.count()).select_from(Punishment)
                .where(Punishment.chat_id == chat.id,
                       Punishment.target_user_id.in_(joiners),
                       Punishment.type == "ban",
                       Punishment.active.is_(True)))
        if int(attempts or 0) >= config.antiraid_limit:
            await apply_punishment(bot, session_factory, chat_id=chat.id, target_id=inviter_id,
                                   action="бан", duration=None,
                                   reason="Антирейд: приглашение забаненного", actor_id=None)


@router.message(F.new_chat_members)
async def on_new_members(message: Message, settings: Settings, session_factory, store: ChatSettingsStore, bot: Bot):
    inviter_id = await _inviter_of(message)
    await _track_members(message, session_factory, joined=True, invited_by=inviter_id)
    config = await store.get(message.chat.id)
    await _enforce_invite_policy(message.chat, [user.id for user in message.new_chat_members],
                                 inviter_id, config, session_factory, bot)
    await store.remember_title(message.chat.id, message.chat.title)
    bots = [user for user in message.new_chat_members if user.is_bot]
    if bots and config.bots_denied:
        for bot_user in bots:
            try:
                await bot.ban_chat_member(message.chat.id, bot_user.id)
            except Exception:
                log.info("Could not remove an invited bot from chat %s", message.chat.id)
        if not [user for user in message.new_chat_members if not user.is_bot]:
            return
    humans = [user for user in message.new_chat_members if not user.is_bot]
    if config.minreg_days:
        humans = await _apply_minreg(message.chat, humans, config, session_factory, bot)
    if not humans:
        return
    tags = await _tags(session_factory, message.chat.id, [user.id for user in humans])
    names = ", ".join(_display_name(user, tags.get(user.id)) for user in humans)
    if config.notify_joins:
        await bot.send_message(message.chat.id, f"🟢 {names} — вход в чат", parse_mode="HTML")
    if config.welcome_text:
        from mellow.chatadmin.welcome import render_welcome
        greeting = render_welcome(config.welcome_text, full_name=humans[0].full_name,
                                  plural=len(humans) > 1)
        if len(humans) > 1:
            greeting = f"{greeting}\n\n{names}"
        await bot.send_message(message.chat.id, greeting, parse_mode="HTML")
    if config.rules_text:
        await bot.send_message(message.chat.id, f"<b>Правила чата</b>\n{html.escape(config.rules_text)}",
                               parse_mode="HTML")
    async with session_factory() as session:
        from mellow.chatadmin.triggers import trigger_actions, trigger_level
        actions = await trigger_actions(session, message.chat.id, "новый участник")
        level = await trigger_level(session, message.chat.id, "новый участник")
    if not actions:
        return
    from mellow.moderation import apply_punishment, cap_duration
    for user in message.new_chat_members:
        if user.is_bot:
            continue
        for action in actions:
            if action.get("command") == "удалить":
                continue
            await apply_punishment(bot, session_factory, chat_id=message.chat.id, target_id=user.id,
                                   action=action["command"],
                                   duration=cap_duration(settings, level, action.get("duration")),
                                   reason=action.get("reason"), actor_id=None)


def _display_name(user, tag: str | None) -> str:
    """Имя участника с личным тегом («+тг тег»), как в документации Ириса."""
    name = html.escape(user.full_name)
    return f"{name} <i>[{html.escape(tag)}]</i>" if tag else name


async def _tags(session_factory, chat_id: int, telegram_ids: list[int]) -> dict[int, str]:
    from mellow.models import ChatMemberActivity
    if not telegram_ids:
        return {}
    async with session_factory() as session:
        rows = (await session.scalars(select(ChatMemberActivity)
                                      .where(ChatMemberActivity.chat_id == chat_id,
                                             ChatMemberActivity.telegram_id.in_(telegram_ids)))).all()
    return {row.telegram_id: row.tag for row in rows if row.tag}


async def _apply_minreg(chat, users, config, session_factory, bot) -> list:
    """«+Минрег»: исключает участников, которые знают бота меньше указанного срока."""
    from mellow.models import User, utcnow
    from mellow.moderation import perform_telegram_action
    kept = []
    async with session_factory() as session:
        rows = {user.telegram_id: user for user in (await session.scalars(
            select(User).where(User.telegram_id.in_([user.id for user in users])))).all()}
    cutoff = utcnow() - timedelta(days=config.minreg_days)
    for user in users:
        stored = rows.get(user.id)
        created = stored.created_at if stored is not None else None
        if created is None:
            kept.append(user)  # о человеке ничего не известно — не наказываем без данных
            continue
        moment = created if created.tzinfo else created.replace(tzinfo=cutoff.tzinfo)
        if moment <= cutoff:
            kept.append(user)
            continue
        try:
            await perform_telegram_action(bot, chat.id, "кик", user.id)
        except Exception:
            log.info("Minreg could not kick %s in chat %s", user.id, chat.id)
    return kept


async def _handle_leave(chat, user, session_factory, store, settings, bot) -> None:
    """Общий обработчик выхода: уведомление, запись выхода и автокик."""
    from mellow.models import ChatLeave, MessageStat, User, utcnow
    if user is None or user.is_bot:
        return
    config = await store.get(chat.id)
    async with session_factory() as session, session.begin():
        session.add(ChatLeave(chat_id=chat.id, telegram_id=user.id, left_at=utcnow()))
    if config.notify_leaves:
        message_count = 0
        async with session_factory() as session:
            row = await session.scalar(select(MessageStat.message_count)
                                       .join(User, MessageStat.user_id == User.id)
                                       .where(User.telegram_id == user.id))
            message_count = int(row or 0)
        if message_count >= (config.leave_notify_min_messages or 0):
            await bot.send_message(chat.id, f"⚪ {html.escape(user.full_name)} — выход из чата",
                                   parse_mode="HTML")
    if not config.autokick_count:
        return
    from mellow.moderation import apply_punishment
    from mellow.services import deactivate_punishments  # noqa: F401 - keeps the import local
    window = timedelta(seconds=config.autokick_window_seconds or 3600)
    async with session_factory() as session:
        rows = (await session.scalars(select(ChatLeave)
                                      .where(ChatLeave.chat_id == chat.id,
                                             ChatLeave.telegram_id == user.id))).all()
    recent = [row for row in rows if _aware(row.left_at) >= utcnow() - window]
    if len(recent) < config.autokick_count:
        return
    async with session_factory() as session, session.begin():
        for row in rows:
            await session.delete(row)
    action = "бан" if (config.autokick_action or "кик") == "бан" else "кик"
    await apply_punishment(bot, session_factory, chat_id=chat.id, target_id=user.id, action=action,
                           duration=None, reason="Автокик: частые выходы", actor_id=None)


def _aware(value) -> datetime:
    if value is None:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@router.message(F.left_chat_member)
async def on_left_member(message: Message, settings: Settings, session_factory, store: ChatSettingsStore,
                         bot: Bot):
    await _track_members(message, session_factory, joined=False)
    await _handle_leave(message.chat, message.left_chat_member, session_factory, store, settings, bot)


@router.chat_member()
async def on_membership_change(update: ChatMemberUpdated, settings: Settings, session_factory,
                               store: ChatSettingsStore, bot: Bot):
    """Joins and leaves are recorded even when the chat hides service messages."""
    member = update.new_chat_member
    if member is None or member.user.is_bot:
        return
    joined = member.status in {"member", "administrator", "creator", "restricted"}
    previous = update.old_chat_member
    was_joined = previous is not None and previous.status in {"member", "administrator", "creator",
                                                              "restricted"}
    if was_joined and not joined:
        await _handle_leave(update.chat, member.user, session_factory, store, settings, bot)
        return
    if joined and not was_joined:
        inviter = update.from_user
        inviter_id = inviter.id if inviter is not None and inviter.id != member.user.id else None
        if inviter_id is not None:
            from mellow.chatadmin.admin_commands import _remember_members
            async with session_factory() as session, session.begin():
                await _remember_members(session, update.chat.id, [member.user.id], True,
                                        invited_by=inviter_id)
            config = await store.get(update.chat.id)
            await _enforce_invite_policy(update.chat, [member.user.id], inviter_id, config,
                                         session_factory, bot)
    async with session_factory() as session, session.begin():
        from mellow.models import ChatMemberActivity, utcnow
        row = await session.get(ChatMemberActivity, (update.chat.id, member.user.id))
        if row is None:
            session.add(ChatMemberActivity(chat_id=update.chat.id, telegram_id=member.user.id,
                                           joined_at=utcnow() if joined else None,
                                           last_message_at=None, is_member=joined))
        else:
            row.is_member = joined
            if joined and row.joined_at is None:
                row.joined_at = utcnow()
            row.updated_at = utcnow()


@router.chat_join_request()
async def on_join_request(update, settings: Settings, session_factory, store: ChatSettingsStore, bot: Bot):
    """«+Автозаявки»: принимает заявки на вступление автоматически."""
    config = await store.get(update.chat.id)
    if not config.auto_join_requests:
        return
    try:
        await bot.approve_chat_join_request(update.chat.id, update.from_user.id)
    except Exception:
        log.info("Could not approve a join request in chat %s", update.chat.id)
        return
    from mellow.chatadmin.admin_commands import _remember_members
    async with session_factory() as session, session.begin():
        await _remember_members(session, update.chat.id, [update.from_user.id], True)


@router.callback_query()
async def inline_notice(callback: CallbackQuery, store: ChatSettingsStore, bot: Bot):
    """«+Инлайны»: бот пишет в чат, кто нажал кнопку (в групповых чатах)."""
    if callback.message is None or callback.message.chat.type not in GROUP_TYPES:
        return
    config = await store.get(callback.message.chat.id)
    if not config.inline_notices:
        return
    if callback.data and callback.data.startswith(("cleanup:", "summon:")):
        # Служебные кнопки модерации уже отвечают сами — не дублируем их уведомлением.
        return
    name = html.escape(callback.from_user.full_name)
    await bot.send_message(callback.message.chat.id, f"🔘 {name} нажал(а) кнопку",
                           parse_mode="HTML")
