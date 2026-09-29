from __future__ import annotations

import html
import logging
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from mellow.chatadmin.commands import handle_chat_command
from mellow.chatadmin.config import ChatSettingsStore
from mellow.config import Settings
from mellow.delivery import (application_event_key, deliver_application, deliver_service_item,
                             service_event_key)
from mellow.keyboards import (MENU_TEXTS, application_edit_fields, application_submit,
                              main_menu, ticket_actions)
from mellow.minecraft import MinecraftClient, MinecraftUnavailable
from mellow.models import (Application, ApplicationDraft, OutboxEvent, Staff,
                           Suggestion, Ticket, User, UserPrompt, WhitelistOperation, utcnow)
from mellow.moderation import (active_warnings, apply_punishment, cap_duration, describe_period,
                               parse_period, perform_telegram_action, punishment_summary)
from mellow.rendering import render_application
from mellow.services import (audit, deactivate_punishments, drop_warnings, ensure_user, hierarchy_allows,
                             permitted, restore_punishments, staff_level)
from mellow.stats import (community_statistics, member_progress, render_community_statistics,
                          render_member_progress)

log = logging.getLogger("mellow.handlers")
router = Router(name="mellow")


def callback_audit_actor(callback: CallbackQuery) -> int | None:
    # Group clicks can be performed in anonymous context. Authorize the update,
    # but don't persist the account ID in audit or reviewer fields.
    if callback.message and callback.message.chat.type in {"group", "supergroup"}:
        return None
    return callback.from_user.id


async def begin_application(message: Message, settings: Settings, session_factory):
    if message.chat.type != "private" or not message.from_user:
        return
    async with session_factory() as session, session.begin():
        user = await ensure_user(session, message.from_user)
        active = await session.scalar(select(Application).where(
            Application.user_id == user.id,
            Application.status.in_(["creating", "pending", "info_requested"])))
        if active:
            active_id = active.id
        else:
            active_id = None
            draft = await session.get(ApplicationDraft, user.id)
            if draft:
                draft.data, draft.question_index, draft.editing_index = {}, 0, None
            else:
                session.add(ApplicationDraft(user_id=user.id, data={}, question_index=0))
    if active_id:
        await message.answer(f"У тебя уже есть активная заявка #{active_id}. Используй «Моя заявка», чтобы посмотреть статус.")
    else:
        await message.answer("<b>Заявка на вступление</b>\n\nСпасибо за интерес к Mellow. Заполни небольшую анкету, после чего администрация рассмотрит её.\n\n" + html.escape(settings.questions[0].label), parse_mode="HTML")


async def start(message: Message, settings: Settings, session_factory):
    if message.chat.type != "private" or not message.from_user:
        return
    async with session_factory() as session, session.begin():
        await ensure_user(session, message.from_user)
        level = await staff_level(session, message.from_user.id)
    await message.answer("<b>Mellow</b>\n\nТёплое место для спокойной игры. Выбери, чем помочь.", reply_markup=main_menu(level > 0), parse_mode="HTML")


@router.message(CommandStart())
async def start_handler(message: Message, settings: Settings, session_factory):
    await start(message, settings, session_factory)


@router.message(Command("chatid"))
async def chat_id_command(message: Message, settings: Settings, session_factory):
    if message.sender_chat is not None:
        return
    if message.chat.type == "private" and message.from_user:
        await message.answer(f"Chat ID: <code>{message.chat.id}</code>")
    elif message.chat.type in {"group", "supergroup"} and message.from_user:
        async with session_factory() as session:
            allowed, _ = await permitted(session, settings, message.from_user.id, "settings")
        if allowed:
            await message.reply(f"Chat ID: <code>{message.chat.id}</code>")


@router.message(Command("id"))
async def private_id(message: Message):
    # Self-service only; never reveal a caller's ID to a group.
    if message.chat.type == "private" and message.from_user:
        await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")


@router.message(Command("cancel"))
async def cancel(message: Message, session_factory):
    if message.chat.type != "private" or not message.from_user:
        return
    async with session_factory() as session, session.begin():
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        if user:
            draft = await session.get(ApplicationDraft, user.id)
            prompt = await session.get(UserPrompt, user.id)
            if draft:
                await session.delete(draft)
            if prompt:
                await session.delete(prompt)
    await message.answer("Действие отменено.")


async def process_application_answer(message: Message, settings: Settings, session_factory) -> bool:
    if not message.from_user or not message.text:
        return False
    answer = message.text.strip()
    # An unfinished draft must never swallow commands, menu buttons or the statistics
    # command: otherwise the user gets stuck with a form that eats every message.
    if answer.startswith("/") or answer in MENU_TEXTS or is_statistics_request(answer):
        return False
    response = None
    async with session_factory() as session, session.begin():
        user = await ensure_user(session, message.from_user)
        draft = await session.scalar(select(ApplicationDraft).where(ApplicationDraft.user_id == user.id).with_for_update())
        if not draft:
            return False
        index = draft.editing_index if draft.editing_index is not None else draft.question_index
        if index >= len(settings.questions):
            return False
        question = settings.questions[index]
        if not answer or len(answer) > question.max_length:
            response = (f"Ответ слишком длинный. Максимум: {question.max_length} символов." if len(answer) > question.max_length else "Ответ не должен быть пустым.") + "\n\n" + question.label
        elif question.key == "minecraft_username" and not re.fullmatch(r"[A-Za-z0-9_]{3,16}", answer):
            response = "Никнейм Minecraft должен содержать 3–16 латинских букв, цифр или символов _."
        elif question.key == "age" and not answer.isdigit():
            response = "Укажи возраст числом."
        else:
            draft.data = {**draft.data, question.key: answer}
            if draft.editing_index is not None:
                draft.editing_index = None
                done = True
            else:
                draft.question_index += 1
                done = draft.question_index >= len(settings.questions)
            data = dict(draft.data)
            next_index = draft.question_index
    if response:
        await message.answer(response)
    elif done:
        await message.answer(render_application(data, settings), reply_markup=application_submit(len(settings.questions)), parse_mode="HTML")
    else:
        await message.answer(html.escape(settings.questions[next_index].label))
    return True


@router.callback_query(F.data == "app:start")
async def app_start_callback(callback: CallbackQuery, settings: Settings, session_factory):
    await begin_application(callback.message, settings, session_factory)
    await callback.answer()


@router.callback_query(F.data == "app:cancel")
async def app_cancel_callback(callback: CallbackQuery, session_factory):
    async with session_factory() as session, session.begin():
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        if user:
            draft = await session.get(ApplicationDraft, user.id)
            if draft:
                await session.delete(draft)
    await callback.message.edit_text("Анкета отменена.")
    await callback.answer()


@router.callback_query(F.data == "app:editmenu")
async def app_edit_menu(callback: CallbackQuery, settings: Settings, session_factory):
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        draft = await session.get(ApplicationDraft, user.id) if user else None
    if not draft:
        await callback.answer("Черновик не найден.", show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=application_edit_fields([q.label for q in settings.questions]))
    await callback.answer()


@router.callback_query(F.data.startswith("app:edit:"))
async def app_edit_field(callback: CallbackQuery, settings: Settings, session_factory):
    index = int(callback.data.rsplit(":", 1)[1])
    if not 0 <= index < len(settings.questions):
        await callback.answer("Поле не найдено.", show_alert=True)
        return
    async with session_factory() as session, session.begin():
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        draft = await session.get(ApplicationDraft, user.id) if user else None
        if draft:
            draft.editing_index = index
    if not draft:
        await callback.answer("Черновик не найден.", show_alert=True)
        return
    await callback.message.answer("Введи новое значение: " + html.escape(settings.questions[index].label))
    await callback.answer()


@router.callback_query(F.data == "app:review")
async def app_review_callback(callback: CallbackQuery, settings: Settings, session_factory):
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        draft = await session.get(ApplicationDraft, user.id) if user else None
        data = dict(draft.data) if draft else None
    if data is None:
        await callback.answer("Черновик не найден.", show_alert=True)
    else:
        await callback.message.edit_text(render_application(data, settings), reply_markup=application_submit(len(settings.questions)), parse_mode="HTML")
        await callback.answer()


async def submit_application(callback: CallbackQuery, settings: Settings, session_factory, bot: Bot):
    """Store the form and make its delivery durable before answering the user.

    The application row and its delivery event are written in one transaction, so the
    form cannot be lost by a Telegram outage, a missing topic right or a restart. The
    immediate attempt below is only a shortcut for a successful case; anything that
    fails is retried by the outbox worker (see :mod:`mellow.delivery`).
    """
    telegram_id = callback.from_user.id
    app_id = None
    pending_question = None
    async with session_factory() as session, session.begin():
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if not user:
            await callback.answer("Начни с /start", show_alert=True)
            return
        existing = await session.scalar(select(Application).where(
            Application.user_id == user.id,
            Application.status.in_(["creating", "pending", "info_requested"])))
        draft = await session.scalar(select(ApplicationDraft)
                                     .where(ApplicationDraft.user_id == user.id).with_for_update())
        if existing:
            await callback.answer("Заявка уже отправлена или обрабатывается.", show_alert=True)
            return
        missing = next((question for question in settings.questions
                        if draft is None or question.key not in (draft.data or {})), None)
        if draft is None or missing is not None:
            # The form can gain a question while a draft is open. Ask for what is missing
            # instead of refusing the submission forever with no way to continue.
            if draft is None:
                draft = ApplicationDraft(user_id=user.id, data={}, question_index=0)
                session.add(draft)
            else:
                draft.question_index = settings.questions.index(missing)
                draft.editing_index = None
            pending_question = missing.label if missing is not None else settings.questions[0].label
        else:
            data = dict(draft.data)
            app = Application(user_id=user.id, status="creating", application_data=data)
            try:
                async with session.begin_nested():
                    session.add(app)
                    await session.flush()
            except IntegrityError:
                await callback.answer("Заявка уже отправляется.", show_alert=True)
                return
            app_id = app.id
            user.minecraft_username = data.get("minecraft_username")
            await session.delete(draft)
            session.add(OutboxEvent(event_key=application_event_key(app_id),
                                    event_type="application_delivery",
                                    payload={"application_id": app_id}, status="pending"))
    if app_id is None:
        await callback.message.answer("Анкета заполнена не полностью. Укажи: " + html.escape(pending_question or ""))
        await callback.answer("Нужен ещё один ответ")
        return
    await callback.answer("Анкета принята")
    result = await deliver_application(bot, settings, session_factory, app_id, announce=False)
    if result.delivered:
        await callback.message.edit_text(f"Заявка #{app_id} отправлена администрации. Мы сообщим о решении здесь.")
    else:
        await callback.message.edit_text(
            f"Заявка #{app_id} принята. Отправляю её администрации — бот повторит попытку автоматически, "
            "отправлять анкету заново не нужно.")


@router.callback_query(F.data == "app:submit")
async def app_submit_callback(callback: CallbackQuery, settings: Settings, session_factory, bot: Bot):
    await submit_application(callback, settings, session_factory, bot)


@router.callback_query(F.data.startswith("app:"))
async def application_action(callback: CallbackQuery, settings: Settings, session_factory, bot: Bot):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Кнопка устарела.", show_alert=True)
        return
    _, action, raw_id = parts
    try:
        app_id = int(raw_id)
    except ValueError:
        await callback.answer("Кнопка устарела.", show_alert=True)
        return
    actor_audit_id = callback_audit_actor(callback)
    async with session_factory() as session:
        allowed, _ = await permitted(session, settings, callback.from_user.id, "applications")
    if not allowed:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    async with session_factory() as session, session.begin():
        allowed, _ = await permitted(session, settings, callback.from_user.id, "applications")
        if not allowed:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        app = await session.get(Application, app_id)
        if not app:
            await callback.answer("Заявка не найдена.", show_alert=True)
            return
        owner = await session.get(User, app.user_id)
        recipient = owner.telegram_id
        expected = ["pending", "info_requested"]
        status = {"accept": "accepted", "reject": "rejected", "info": "info_requested", "close": "closed"}.get(action)
        if not status:
            await callback.answer("Действие не поддерживается.", show_alert=True)
            return
        if action == "info":
            expected = ["pending"]
        if action == "close":
            expected = ["pending", "info_requested", "accepted", "rejected"]
        result = await session.execute(update(Application).where(Application.id == app_id, Application.status.in_(expected)).values(
            status=status, reviewed_by=actor_audit_id, updated_at=utcnow()))
        if result.rowcount != 1:
            await callback.answer("Заявка уже обработана.", show_alert=True)
            return
        await audit(session, f"application_{action}", actor_audit_id, f"application:{app_id}", {"status": status})
        prompt = await session.get(UserPrompt, owner.id)
        if action == "info":
            if prompt:
                prompt.kind = f"application_info:{app_id}"
            else:
                session.add(UserPrompt(user_id=owner.id, kind=f"application_info:{app_id}"))
        elif prompt and prompt.kind == f"application_info:{app_id}":
            await session.delete(prompt)
    if action == "info":
        try:
            await bot.send_message(recipient, f"По заявке #{app_id} нужна дополнительная информация. Отправь ответ следующим сообщением — мы передадим его администрации.")
        except TelegramForbiddenError:
            pass
        await callback.answer("Запрос отправлен")
        return
    messages = {"accept": "Заявка принята. Администрация свяжется с тобой с дальнейшими шагами.",
                "reject": "По итогам рассмотрения заявка отклонена.", "close": "Заявка закрыта."}
    try:
        await bot.send_message(recipient, messages[action])
    except TelegramForbiddenError:
        pass
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.answer("Готово")


async def show_my_application(message: Message, session_factory):
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        app = await session.scalar(select(Application).where(Application.user_id == user.id).order_by(Application.id.desc())) if user else None
    if not app:
        await message.answer("У тебя пока нет заявок.")
        return
    status = {"creating": "готовится к отправке", "pending": "на рассмотрении", "info_requested": "нужна дополнительная информация",
              "accepted": "принята", "rejected": "отклонена", "closed": "закрыта"}.get(app.status, app.status)
    await message.answer(f"Заявка #{app.id}\nСтатус: {status}")


async def set_prompt(message: Message, session_factory, kind: str):
    async with session_factory() as session, session.begin():
        user = await ensure_user(session, message.from_user)
        prompt = await session.get(UserPrompt, user.id)
        if prompt:
            prompt.kind = kind
        else:
            session.add(UserPrompt(user_id=user.id, kind=kind))
    text = {"support": "Напиши тему и описание технической проблемы одним сообщением. Первая строка станет темой.",
            "suggestion": "Напиши предложение одним сообщением.",
            "administration": "Напиши тему и текст обращения одним сообщением. Первая строка станет темой."}[kind]
    await message.answer(text + "\n\nДля отмены отправь /cancel.")


async def show_open_tickets(message: Message, session_factory):
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        items = list((await session.scalars(select(Ticket).where(Ticket.user_id == user.id, Ticket.status.in_(["open", "review"]))
                                             .order_by(Ticket.id.desc()).limit(10))).all()) if user else []
        closed = list((await session.scalars(select(Ticket).where(Ticket.user_id == user.id, Ticket.status == "closed")
                                              .order_by(Ticket.id.desc()).limit(5))).all()) if user else []
    if not items and not closed:
        await message.answer("Обращений пока нет.")
        return
    rows = [[InlineKeyboardButton(text=f"#{item.id} · {item.subject[:35]}", callback_data=f"ticket:reply:{item.id}")] for item in items]
    rows.extend([[InlineKeyboardButton(text=f"Открыть заново #{item.id}", callback_data=f"ticket:reopen:{item.id}")] for item in closed])
    prompt = "Выбери обращение, чтобы написать администрации:"
    if closed:
        prompt += "\nЗакрытые обращения можно открыть повторно."
    await message.answer(prompt, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("ticket:reopen:"))
async def ticket_reopen(callback: CallbackQuery, session_factory, bot: Bot):
    ticket_id = int(callback.data.rsplit(":", 1)[1])
    async with session_factory() as session, session.begin():
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        ticket = await session.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
        if not user or not ticket or ticket.user_id != user.id or ticket.status != "closed":
            await callback.answer("Обращение нельзя открыть повторно.", show_alert=True)
            return
        if not ticket.chat_id or not ticket.thread_id:
            await callback.answer("Тема обращения недоступна. Напиши администрации новое обращение.", show_alert=True)
            return
        ticket.status = "open"
        ticket.closed_at = None
        await audit(session, "ticket_reopened_by_user", callback.from_user.id, f"ticket:{ticket_id}")
        chat_id, thread_id = ticket.chat_id, ticket.thread_id
    try:
        await bot.send_message(chat_id, f"Пользователь повторно открыл обращение #{ticket_id}.",
                               message_thread_id=thread_id,
                               reply_markup=ticket_actions("ticket", ticket_id, "open"))
    except Exception:
        log.exception("Could not notify staff about reopened ticket id=%s", ticket_id)
    await callback.answer("Обращение снова открыто")


@router.callback_query(F.data.startswith("ticket:reply:"))
async def ticket_reply_start(callback: CallbackQuery, session_factory):
    ticket_id = int(callback.data.rsplit(":", 1)[1])
    async with session_factory() as session, session.begin():
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        ticket = await session.get(Ticket, ticket_id)
        if not user or not ticket or ticket.user_id != user.id or ticket.status not in {"open", "review"}:
            await callback.answer("Обращение недоступно.", show_alert=True)
            return
        prompt = await session.get(UserPrompt, user.id)
        if prompt:
            prompt.kind = f"ticket_reply:{ticket_id}"
        else:
            session.add(UserPrompt(user_id=user.id, kind=f"ticket_reply:{ticket_id}"))
    await callback.message.answer(f"Напиши сообщение для обращения #{ticket_id}. Для отмены — /cancel.")
    await callback.answer()


@router.callback_query(F.data.startswith("ticket:") | F.data.startswith("suggestion:"))
async def service_item_action(callback: CallbackQuery, settings: Settings, session_factory, bot: Bot):
    actor_audit_id = callback_audit_actor(callback)
    _, action, raw_id = callback.data.split(":", 2)
    item_id = int(raw_id)
    suggestion = callback.data.startswith("suggestion:")
    permission = "suggestions" if suggestion else "tickets"
    async with session_factory() as session:
        allowed, _ = await permitted(session, settings, callback.from_user.id, permission)
    if not allowed:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    model = Suggestion if suggestion else Ticket
    async with session_factory() as session, session.begin():
        allowed, _ = await permitted(session, settings, callback.from_user.id, permission)
        if not allowed:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        item = await session.scalar(select(model).where(model.id == item_id).with_for_update())
        if not item:
            await callback.answer("Запись не найдена.", show_alert=True)
            return
        valid = {"new", "review"} if suggestion else {"open", "review"}
        if item.status not in valid:
            await callback.answer("Статус уже изменён.", show_alert=True)
            return
        if action == "review" and item.status == ("new" if suggestion else "open"):
            item.status = "review"
            if not suggestion:
                item.assigned_to = actor_audit_id
        elif suggestion and action in {"accept", "reject", "implemented"}:
            if action == "implemented" and item.status != "review":
                await callback.answer("Сначала переведи предложение в рассмотрение.", show_alert=True)
                return
            item.status = {"accept": "accepted", "reject": "rejected", "implemented": "implemented"}[action]
            item.decision = {"accept": "Принято администрацией", "reject": "Отклонено администрацией",
                             "implemented": "Предложение реализовано"}[action]
        elif not suggestion and action == "close":
            item.status = "closed"
            item.closed_at = utcnow()
        else:
            await callback.answer("Кнопка устарела.", show_alert=True)
            return
        status = item.status
        await audit(session, f"{permission}_{action}", actor_audit_id, f"{permission}:{item_id}", {"status": status})
        owner = await session.get(User, item.user_id)
        owner_telegram_id = owner.telegram_id
    terminal = status in {"accepted", "rejected", "implemented", "closed"}
    if terminal:
        label = {"accepted": "принято", "rejected": "отклонено", "implemented": "реализовано", "closed": "закрыто"}[status]
        try:
            await bot.send_message(owner_telegram_id, f"Обращение #{item_id}: {label}.")
        except TelegramForbiddenError:
            pass
    try:
        await callback.message.edit_reply_markup(reply_markup=ticket_actions("suggestion" if suggestion else "ticket", item_id, status))
    except TelegramBadRequest:
        pass
    await callback.answer("Статус обновлён")


@router.callback_query(F.data.startswith("threshold:"))
async def threshold_action(callback: CallbackQuery, settings: Settings, session_factory, minecraft: MinecraftClient, bot: Bot):
    actor_audit_id = callback_audit_actor(callback)
    _, action, raw_id = callback.data.split(":", 2)
    telegram_id = int(raw_id)
    async with session_factory() as session:
        allowed, _ = await permitted(session, settings, callback.from_user.id, "whitelist")
    if not allowed:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    async with session_factory() as session, session.begin():
        allowed, _ = await permitted(session, settings, callback.from_user.id, "whitelist")
        if not allowed:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if not user:
            await callback.answer("Участник не найден.", show_alert=True)
            return
        if action in {"no", "later"}:
            await audit(session, f"whitelist_{action}", actor_audit_id, f"telegram:{telegram_id}")
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer("Отмечено")
            return
        if action != "add":
            await callback.answer("Кнопка устарела.", show_alert=True)
            return
        username = user.minecraft_username
        if not username:
            await callback.answer("Minecraft-ник не найден. Участник должен отправить заявку.", show_alert=True)
            return
        operation_key = f"threshold-{user.id}"
        operation = await session.scalar(select(WhitelistOperation).where(WhitelistOperation.operation_key == operation_key))
        if operation is None:
            candidate = WhitelistOperation(operation_key=operation_key, username=username, user_id=telegram_id, status="pending")
            try:
                async with session.begin_nested():
                    session.add(candidate)
                    await session.flush()
                operation = candidate
            except IntegrityError:
                operation = await session.scalar(select(WhitelistOperation).where(WhitelistOperation.operation_key == operation_key))
        if operation and operation.status == "success":
            await callback.answer("Уже подтверждено.", show_alert=True)
            return
    try:
        result = await minecraft.add_to_whitelist(username, operation_key)
    except MinecraftUnavailable:
        async with session_factory() as session, session.begin():
            await session.execute(update(WhitelistOperation).where(
                WhitelistOperation.operation_key == operation_key,
                WhitelistOperation.status != "success").values(
                    status="unknown", result="Сервер не подтвердил выполнение; повторный запрос с тем же ключом безопасен."))
        await callback.answer("Minecraft-сервер не подтвердил выполнение. Можно повторить позже.", show_alert=True)
        return
    async with session_factory() as session, session.begin():
        result_update = await session.execute(update(WhitelistOperation).where(
            WhitelistOperation.operation_key == operation_key,
            WhitelistOperation.status != "success").values(status="success", result=result, updated_at=utcnow()))
        if result_update.rowcount == 1:
            await audit(session, "whitelist_add", actor_audit_id, f"minecraft:{username}", {"result": result, "operation_key": operation_key})
    await callback.message.edit_reply_markup(reply_markup=None)
    if result == "ALREADY_WHITELISTED":
        confirmation = f"{html.escape(username)} уже находится в whitelist. Minecraft подтвердил состояние."
    else:
        confirmation = f"Minecraft подтвердил добавление {html.escape(username)} в whitelist."
    await callback.message.answer(confirmation)
    await callback.answer("Whitelist обновлён")


async def relay_prompt(message: Message, settings: Settings, session_factory, bot: Bot) -> bool:
    if not message.from_user or not message.text:
        return False
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        prompt = await session.get(UserPrompt, user.id) if user else None
        if not prompt:
            return False
        user_id, kind = user.id, prompt.kind
        telegram_id = user.telegram_id
    content = message.text.strip()
    if len(content) < 3 or len(content) > 4000:
        await message.answer("Сообщение должно содержать от 3 до 4000 символов.")
        return True
    if kind.startswith("ticket_reply:"):
        ticket_id = int(kind.split(":", 1)[1])
        async with session_factory() as session:
            ticket = await session.get(Ticket, ticket_id)
        if not ticket or ticket.user_id != user_id or ticket.status not in {"open", "review"}:
            await message.answer("Обращение уже закрыто.")
            return True
        if not ticket.chat_id or not ticket.thread_id:
            await message.answer("Тикет временно недоступен для ответа.")
            return True
        try:
            await bot.send_message(ticket.chat_id, f"<b>Сообщение пользователя по тикету #{ticket_id}</b>\n{html.escape(content)}",
                                   message_thread_id=ticket.thread_id, parse_mode="HTML")
        except Exception:
            log.exception("Could not relay a user ticket message, id=%s", ticket_id)
            await message.answer("Не удалось передать сообщение. Попробуй позже.")
            return True
        async with session_factory() as session, session.begin():
            current = await session.get(UserPrompt, user_id)
            if current:
                await session.delete(current)
        await message.answer("Сообщение передано администрации.")
        return True
    if kind.startswith("application_info:"):
        app_id = int(kind.split(":", 1)[1])
        async with session_factory() as session:
            app = await session.get(Application, app_id)
        if not app or app.user_id != user_id or app.status != "info_requested":
            await message.answer("Запрос дополнительной информации уже закрыт.")
            return True
        if not app.chat_id or not app.thread_id:
            await message.answer("Заявка временно недоступна. Администрация проверит её вручную.")
            return True
        try:
            await bot.send_message(app.chat_id, f"<b>Дополнение к заявке #{app_id}</b>\n{html.escape(content)}",
                                   message_thread_id=app.thread_id, parse_mode="HTML")
        except Exception:
            log.exception("Could not relay application follow-up, id=%s", app_id)
            await message.answer("Не удалось передать ответ. Попробуй позже.")
            return True
        async with session_factory() as session, session.begin():
            current = await session.get(Application, app_id)
            current_prompt = await session.get(UserPrompt, user_id)
            if current and current.status == "info_requested":
                current.status = "pending"
                current.updated_at = utcnow()
                await audit(session, "application_info_reply", telegram_id, f"application:{app_id}")
            if current_prompt:
                await session.delete(current_prompt)
        await message.answer("Ответ передан администрации. Заявка снова на рассмотрении.")
        return True
    if kind not in {"support", "suggestion", "administration"}:
        return False
    if kind == "suggestion":
        subject, body = "Предложение", content
    else:
        lines = content.splitlines()
        subject = (lines[0].strip() or "Обращение")[:100]
        body = "\n".join(lines[1:]).strip() or subject
    item_kind = "suggestion" if kind == "suggestion" else "ticket"
    async with session_factory() as session, session.begin():
        current = await session.scalar(select(UserPrompt).where(UserPrompt.user_id == user_id).with_for_update())
        if not current or current.kind != kind:
            await message.answer("Запрос изменился. Начни его заново из меню.")
            return True
        if kind == "suggestion":
            item = Suggestion(user_id=user_id, body=body, status="new")
        else:
            item = Ticket(user_id=user_id, type="support" if kind == "support" else "administration",
                          subject=subject, body=body, status="open")
        session.add(item)
        await session.flush()
        item_id = item.id
        # The message is kept by the durable queue together with the record: a chat without
        # topics or a momentary Telegram failure delays the delivery, it never drops it.
        session.add(OutboxEvent(event_key=service_event_key(item_kind, item_id),
                                event_type="ticket_delivery",
                                payload={"kind": item_kind, "id": item_id}, status="pending"))
        await session.delete(current)
    result = await deliver_service_item(bot, settings, session_factory, item_kind, item_id, announce=False)
    label = "Предложение" if item_kind == "suggestion" else "Обращение"
    if result.delivered:
        await message.answer(f"{label} #{item_id} отправлено. Администрация ответит здесь.")
    else:
        await message.answer(f"{label} #{item_id} принято. Бот доставит его администрации автоматически — "
                             "отправлять заново не нужно.")
    return True


SERVICE_CARD_RE = re.compile(r"^(?:Тикет|Обращение)\s+#(\d+)")


async def find_replied_ticket(session_factory, chat_id: int, thread_id: int | None,
                              reply_to: Message | None, bot: Bot) -> Ticket | None:
    """Find the ticket a staff message answers.

    A forum topic is the natural key. When the chat has no topics everything is posted to
    the chat root, so the ticket is recognised by Telegram's reply to the bot's own card —
    otherwise staff replies would silently go nowhere in a chat without topics.
    """
    async with session_factory() as session:
        if thread_id:
            return await session.scalar(select(Ticket).where(Ticket.chat_id == chat_id,
                Ticket.thread_id == thread_id, Ticket.status.in_(["open", "review"])))
        if reply_to is None or reply_to.from_user is None or not reply_to.from_user.is_bot or not reply_to.text:
            return None
        if bot is not None and reply_to.from_user.id != (await bot.me()).id:
            return None
        match = SERVICE_CARD_RE.match(reply_to.text.strip())
        if match is None:
            return None
        return await session.scalar(select(Ticket).where(Ticket.id == int(match.group(1)),
            Ticket.chat_id == chat_id, Ticket.status.in_(["open", "review"])))


async def handle_support_admin_reply(message: Message, settings: Settings, session_factory, bot: Bot):
    # Group-authored text is not relayed: it may contain identifying details.
    if message.sender_chat is not None or message.from_user is None:
        return
    if not message.text or message.text.lstrip().startswith("/"):
        return
    if message.from_user.is_bot or MODERATION_RE.match(message.text.strip()):
        return
    if message.chat.id not in {settings.support_destination, settings.administration_chat_id}:
        return
    async with session_factory() as session:
        allowed, _ = await permitted(session, settings, message.from_user.id, "tickets")
    if not allowed:
        return
    ticket = await find_replied_ticket(session_factory, message.chat.id, message.message_thread_id,
                                       message.reply_to_message, bot)
    if ticket is None:
        return
    async with session_factory() as session:
        owner = await session.get(User, ticket.user_id)
        recipient = owner.telegram_id if owner else None
        ticket_id = ticket.id
    if recipient:
        try:
            await bot.send_message(recipient, f"<b>Ответ администрации по обращению #{ticket_id}</b>\n"
                                              f"{html.escape(message.text[:3500])}", parse_mode="HTML")
        except TelegramForbiddenError:
            pass
        except Exception:
            log.exception("Could not relay a service reply, ticket id=%s", ticket_id)


STATISTICS_WORDS = frozenset({"статистика", "stats", "stat", "statistics"})
SHORT_STATISTICS_WORDS = frozenset({"стата"})


def is_statistics_request(text: str, *, include_short: bool = False) -> bool:
    """True for «статистика», «📊 Статистика», «/статистика@bot» and «stats».

    «Стата» is deliberately excluded in chats: there it belongs to the chat layer
    («Статистика сообщений»), while «статистика» answers with the board and the
    remaining messages of the caller.
    """
    value = text.strip().casefold()
    if value in {"📊 статистика", "статистика"}:
        return True
    words = value.split(maxsplit=1)
    if not words:
        return False
    word = words[0].lstrip("/!").split("@", 1)[0]
    return word in STATISTICS_WORDS or (include_short and word in SHORT_STATISTICS_WORDS)


async def send_statistics(message: Message, settings: Settings, session_factory, *, in_group: bool) -> None:
    """The chat board plus the caller's own numbers, for everyone who asks.

    Staff are shown without a personal block on purpose: they are excluded from the
    counter, so "осталось написать" would be meaningless for them.
    """
    if message.from_user is None or message.sender_chat is not None:
        return
    async with session_factory() as session:
        level = await staff_level(session, message.from_user.id)
        community = await community_statistics(session, settings, limit=settings.stats_top_limit)
        progress = None if level > 0 else await member_progress(session, settings, message.from_user.id)
    if progress is None or not settings.message_requirement_enabled:
        text = render_community_statistics(community, settings)
    else:
        text = "\n\n".join([render_member_progress(progress, settings),
                            render_community_statistics(community, settings)])
    await message.answer(text, parse_mode="HTML", reply_to_message_id=message.message_id if in_group else None)


async def chat_config_from_factory(session_factory, chat_id: int):
    from mellow.chatadmin.config import chat_config

    async with session_factory() as session:
        return await chat_config(session, chat_id)


async def resolve_moderation_target(message: Message, raw: str, session_factory) -> tuple[int | None, list[str]]:
    """Find the target of a legacy moderation command and return the leftover tokens."""
    tokens = raw.split()
    if message.reply_to_message is not None:
        replied = message.reply_to_message
        if replied.sender_chat is None and replied.from_user is not None:
            return replied.from_user.id, tokens
        return None, tokens
    if not tokens:
        return None, tokens
    target = tokens.pop(0)
    if target.startswith("@"):
        async with session_factory() as session:
            user = await session.scalar(select(User).where(User.username.ilike(target[1:])))
        return (user.telegram_id if user else None), tokens
    if target.isdigit():
        return int(target), tokens
    return None, tokens


@router.message(Command("статистика", "stats", "stat", "statistics", ignore_case=True))
async def statistics_command(message: Message, settings: Settings, session_factory):
    await send_statistics(message, settings, session_factory,
                          in_group=message.chat.type in {"group", "supergroup"})


@router.message(F.text)
async def messages(message: Message, settings: Settings, session_factory, bot: Bot, minecraft: MinecraftClient,
                   store: ChatSettingsStore, recent: object):
    if message.chat.type in {"group", "supergroup"}:
        if message.text and not message.text.lstrip().startswith("/") and is_statistics_request(message.text):
            await send_statistics(message, settings, session_factory, in_group=True)
            return
        # Chat administration («варн», «мут», «+триггер», «чат стата», …) runs first: a
        # moderation keyword must win over the ticket-reply relay.
        if await handle_chat_command(message, settings, session_factory, store, recent, bot):
            return
        await handle_support_admin_reply(message, settings, session_factory, bot)
        await handle_moderation(message, settings, session_factory, bot)
        return
    if message.chat.type != "private" or not message.from_user:
        return
    if not message.text.lstrip().startswith("/") and is_statistics_request(message.text, include_short=True):
        await send_statistics(message, settings, session_factory, in_group=False)
        return
    if await process_application_answer(message, settings, session_factory):
        return
    if await relay_prompt(message, settings, session_factory, bot):
        return
    text = message.text.strip()
    if text in {"🎮 Подать заявку", "Подать заявку"}:
        await begin_application(message, settings, session_factory)
    elif text in {"📋 Моя заявка", "Моя заявка"}:
        await show_my_application(message, session_factory)
    elif text in {"🛠 Техническая поддержка", "Техническая поддержка"}:
        await set_prompt(message, session_factory, "support")
    elif text in {"💡 Предложить идею", "Предложить идею"}:
        await set_prompt(message, session_factory, "suggestion")
    elif text in {"👤 Обратиться к администрации", "Обратиться к администрации"}:
        await set_prompt(message, session_factory, "administration")
    elif text in {"💬 Мои обращения", "Мои обращения"}:
        await show_open_tickets(message, session_factory)
    elif text in {"ℹ️ Информация", "Информация"}:
        await message.answer("Mellow — приватный Minecraft-сервер и спокойное сообщество. Подай заявку, чтобы администрация могла познакомиться с тобой.")
    elif text == "🛡 Мои права":
        async with session_factory() as session:
            level = await staff_level(session, message.from_user.id)
        await message.answer(f"Уровень {level}: {html.escape(settings.levels[level].name)}" if level else "У тебя нет административных прав.")
    else:
        await message.answer("Выбери раздел в меню или отправь /cancel, чтобы отменить текущий ввод.", reply_markup=main_menu())


async def handle_staff_command(message: Message, settings: Settings, session_factory, match):
    actor_id = message.from_user.id
    command, target_raw, level_raw = match.groups()
    target_id = int(target_raw)
    async with session_factory() as session:
        allowed, actor_level = await permitted(session, settings, actor_id, "staff")
        target_level = await staff_level(session, target_id)
    if not allowed:
        await message.reply("Назначать и изменять уровни может только персонал с соответствующим правом.")
        return
    if actor_id == target_id or not hierarchy_allows(actor_level, target_level):
        await message.reply("Нельзя менять собственные права или изменять администратора равного/старшего уровня.")
        return
    if command.lower() == "назначитьадмина" and (not level_raw or int(level_raw) not in settings.levels):
        await message.reply("Формат: назначитьадмина <Telegram ID> <уровень 1–5>.")
        return
    new_level = int(level_raw) if level_raw else None
    async with session_factory() as session, session.begin():
        allowed, actor_level = await permitted(session, settings, actor_id, "staff")
        target_level = await staff_level(session, target_id)
        if not allowed or actor_id == target_id or not hierarchy_allows(actor_level, target_level):
            await message.reply("Недостаточно прав или недопустимое изменение уровня.")
            return
        user = await session.scalar(select(User).where(User.telegram_id == target_id))
        if command.lower() == "назначитьадмина":
            if user is None:
                user = User(telegram_id=target_id)
                session.add(user)
                await session.flush()
            staff = await session.get(Staff, user.id)
            if staff is None:
                session.add(Staff(user_id=user.id, level=new_level, active=True))
            else:
                staff.level, staff.active, staff.updated_at = new_level, True, utcnow()
            await audit(session, "staff_level_set", actor_id, f"telegram:{target_id}", {"old_level": target_level, "new_level": new_level})
        else:
            staff = await session.get(Staff, user.id) if user else None
            if not staff or not staff.active:
                await message.reply("Пользователь не является администратором.")
                return
            staff.active, staff.updated_at = False, utcnow()
            await audit(session, "staff_removed", actor_id, f"telegram:{target_id}", {"old_level": target_level})
    if command.lower() == "назначитьадмина":
        await message.reply(f"Уровень администратора обновлён: {target_id} → {new_level} ({html.escape(settings.levels[new_level].name)}).")
    else:
        await message.reply(f"Административные права сняты с пользователя {target_id}.")


MODERATION_RE = re.compile(r"^/?(бан|разбан|кик|мут|размут|варн|снятьварн|предупреждения)(?:@\w+)?(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
ROLE_RE = re.compile(r"^/?(назначитьадмина|снятьадмина)\s+(\d+)(?:\s+(\d+))?$", re.IGNORECASE)


async def handle_moderation(message: Message, settings: Settings, session_factory, bot: Bot):
    """Slash-command forms of the moderation commands.

    The natural spellings («мут 30 минут @ник», «варн @ник Флуд»), prefixes and the rank
    aliases are handled by :mod:`mellow.chatadmin`; this handler keeps the documented
    slash variants working and shares the same storage and hierarchy rules.
    """
    # Sender-chat messages are not attributed to a user and are not commands.
    if message.sender_chat is not None:
        return
    if message.chat.type not in {"group", "supergroup"} or message.from_user is None or not message.text:
        return
    role_match = ROLE_RE.fullmatch(message.text.strip())
    if role_match:
        await handle_staff_command(message, settings, session_factory, role_match)
        return
    match = MODERATION_RE.match(message.text.strip())
    if not match:
        return
    command, raw = match.group(1).lower(), (match.group(2) or "").strip()
    actor_id = message.from_user.id
    permission = {"бан": "ban", "разбан": "unpunish", "кик": "kick", "мут": "mute", "размут": "unpunish",
                  "варн": "warn", "снятьварн": "unpunish", "предупреждения": "warn"}[command]
    async with session_factory() as session:
        allowed, actor_level = await permitted(session, settings, actor_id, permission)
    if not allowed:
        await message.reply("Недостаточно прав для этой команды.")
        return
    target_id, tokens = await resolve_moderation_target(message, raw, session_factory)
    if target_id is None:
        await message.reply("Укажи пользователя: команда @username [срок] [причина], либо ответь на его сообщение.")
        return
    if command == "предупреждения":
        async with session_factory() as session:
            warns = await active_warnings(session, target_id)
        summary = "\n".join(punishment_summary(row) for row in warns) or "Активных предупреждений нет."
        await message.reply(f"Предупреждения пользователя {target_id}:\n{summary}")
        return
    if command == "снятьварн":
        removed = await drop_warnings(session_factory, actor_id, message.chat.id, target_id, 1)
        await message.reply(f"Снято предупреждений: {removed}.")
        return
    if command in {"разбан", "размут"}:
        action = "разбан" if command == "разбан" else "размут"
        ptype = "ban" if action == "разбан" else "mute"
        removed = await deactivate_punishments(session_factory, actor_id, message.chat.id, target_id, ptype)
        try:
            await perform_telegram_action(bot, message.chat.id, action, target_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            await restore_punishments(session_factory, removed)
            await message.reply("Telegram не подтвердил действие. Проверь права бота и статус пользователя.")
            return
        await message.reply(f"Готово: {command} → {target_id}")
        return

    duration = None
    reason = " ".join(tokens).strip() or None
    if command in {"бан", "мут"} and tokens and re.fullmatch(r"\d+\s*[a-zа-я]*", tokens[0].lower()):
        try:
            duration = parse_period(" ".join(tokens))
            reason = None
        except ValueError as exc:
            await message.reply(str(exc))
            return
    if command in {"бан", "мут"} and duration is None:
        config = await chat_config_from_factory(session_factory, message.chat.id)
        default = config.ban_default_seconds if command == "бан" else config.mute_default_seconds
        duration = default
    maximum = settings.levels[actor_level].max_punishment_seconds
    duration = cap_duration(settings, actor_level, duration)
    async with session_factory() as session:
        target_level = await staff_level(session, target_id)
    if not hierarchy_allows(actor_level, target_level):
        await message.reply("Нельзя применять действие к администратору своего или более высокого уровня.")
        return
    if maximum and duration and duration > maximum:
        await message.reply("Срок превышает лимит твоего уровня.")
        return
    if command == "кик":
        reason = " ".join(tokens).strip() or None
    result = await apply_punishment(bot, session_factory, chat_id=message.chat.id, target_id=target_id,
                                    action=command, duration=duration, reason=reason, actor_id=actor_id)
    if not result.applied:
        await message.reply("Telegram не подтвердил действие. Проверь права бота и статус пользователя.")
        return
    await message.reply(f"Готово: {command} → {target_id}" + (f" ({describe_period(duration)})" if duration else ""))
