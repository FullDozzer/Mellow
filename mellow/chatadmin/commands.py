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

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message

from mellow.chatadmin.admin_commands import *  # noqa: F401,F403 - registers handlers
from mellow.chatadmin.config import ChatSettingsStore, may_use
from mellow.chatadmin.context import GROUP_TYPES, TABLE, ChatContext, strip_prefix
from mellow.chatadmin.moderation_commands import *  # noqa: F401,F403 - registers handlers
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
            allowed, level, required = await may_use(session, message.chat.id, message.from_user.id,
                                                     key_group or key)
        else:
            allowed, required = True, 0
    if not allowed:
        if level > 0:
            await message.reply(f"Недостаточно прав: команда «{html.escape(key)}» доступна с {required} уровня "
                                f"(сейчас у тебя {level}).", parse_mode="HTML")
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


async def _track_members(message, session_factory, *, joined: bool) -> None:
    from mellow.chatadmin.admin_commands import _remember_members

    users = message.new_chat_members if joined else [message.left_chat_member]
    ids = [user.id for user in (users or []) if user is not None and not user.is_bot]
    if not ids:
        return
    async with session_factory() as session, session.begin():
        await _remember_members(session, message.chat.id, ids, joined)


@router.message(F.new_chat_members)
async def on_new_members(message: Message, settings: Settings, session_factory, store: ChatSettingsStore, bot: Bot):
    await _track_members(message, session_factory, joined=True)
    config = await store.get(message.chat.id)
    await store.remember_title(message.chat.id, message.chat.title)
    names = ", ".join(html.escape(user.full_name) for user in message.new_chat_members if not user.is_bot)
    if names and config.welcome_text:
        await bot.send_message(message.chat.id, f"{html.escape(config.welcome_text)}\n\n{names}",
                               parse_mode="HTML")
    if names and config.rules_text:
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


@router.message(F.left_chat_member)
async def on_left_member(message: Message, session_factory):
    await _track_members(message, session_factory, joined=False)


@router.chat_member()
async def on_membership_change(update: ChatMemberUpdated, session_factory):
    """Joins and leaves are recorded even when the chat hides service messages."""
    member = update.new_chat_member
    if member is None or member.user.is_bot:
        return
    joined = member.status in {"member", "administrator", "creator", "restricted"}
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
