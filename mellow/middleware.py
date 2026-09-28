import asyncio
import logging
from aiogram import BaseMiddleware, Bot
from aiogram.types import Update
from mellow.config import Settings
from mellow.keyboards import threshold_actions
from mellow.models import MessageStat, OutboxEvent, User, utcnow
from mellow.privacy import is_anonymous_or_group_authored
from mellow.services import claim_update, increment_message_count
from sqlalchemy import select

log = logging.getLogger("mellow.stats")


class PrivacySafeMessageCounter(BaseMiddleware):
    def __init__(self, settings: Settings, session_factory):
        self.settings = settings
        self.session_factory = session_factory

    async def __call__(self, handler, event: Update, data: dict):
        message = event.message
        # Anonymous/group-authored posts are not inspected beyond sender presence,
        # never counted and never linked to a human. Only an unassociated update ID
        # is claimed for duplicate suppression.
        if message is not None and is_anonymous_or_group_authored(message):
            try:
                if not await claim_update(self.session_factory, event.update_id):
                    return None
            except Exception:
                log.exception("Anonymous/group-authored update could not be claimed")
                return None
            return await handler(event, data)

        try:
            if (message is not None and message.chat.type in {"group", "supergroup"}
                    and message.from_user is not None and not message.from_user.is_bot):
                result = await increment_message_count(self.session_factory, self.settings,
                    message.from_user.id, message.from_user.username, event.update_id)
                _, _, duplicate = result
                if duplicate:
                    return None
            elif not await claim_update(self.session_factory, event.update_id):
                return None
        except Exception:
            # Never log update/message/user objects; they can contain personal data.
            log.exception("Update processing could not be claimed")
            return None
        return await handler(event, data)


async def dispatch_outbox_once(bot: Bot, settings: Settings, session_factory) -> bool:
    """Claim and deliver one durable event. The sending state prevents duplicate sends on restart."""
    async with session_factory() as session, session.begin():
        event = await session.scalar(select(OutboxEvent).where(OutboxEvent.status == "pending")
                                     .order_by(OutboxEvent.id).limit(1).with_for_update(skip_locked=True))
        if event is None:
            return False
        event.status = "sending"
        event.updated_at = utcnow()
        event_id, event_type, payload = event.id, event.event_type, dict(event.payload or {})
    try:
        if event_type != "message_threshold":
            raise RuntimeError("unsupported outbox event")
        async with session_factory() as session:
            user = await session.get(User, int(payload["user_id"]))
            stat = await session.get(MessageStat, user.id) if user else None
            if not user or not stat:
                raise RuntimeError("threshold recipient is missing")
            username, telegram_id = user.username, user.telegram_id
            minecraft_name, count = user.minecraft_username, stat.message_count
        name = f"@{username}" if username else f"ID {telegram_id}"
        text = ("<b>Порог сообщений достигнут</b>\n\n"
                f"Участник: {name}\nMinecraft: {minecraft_name or 'не указан'}\n"
                f"Сообщений: {count}\n\nПользователь выполнил условие для добавления в whitelist.")
        await bot.send_message(settings.administration_chat_id, text,
                               reply_markup=threshold_actions(telegram_id), parse_mode="HTML")
    except Exception as exc:
        async with session_factory() as session, session.begin():
            event = await session.get(OutboxEvent, event_id)
            if event:
                event.status = "failed"
                event.last_error = type(exc).__name__
                event.updated_at = utcnow()
        log.warning("Outbox delivery failed, event_id=%s error_type=%s", event_id, type(exc).__name__)
        return True
    async with session_factory() as session, session.begin():
        event = await session.get(OutboxEvent, event_id)
        if event:
            event.status = "sent"
            event.last_error = None
            event.updated_at = utcnow()
    return True


async def outbox_worker(bot: Bot, settings: Settings, session_factory):
    while True:
        try:
            delivered_or_claimed = await dispatch_outbox_once(bot, settings, session_factory)
        except Exception:
            log.exception("Outbox worker could not process an event")
            await asyncio.sleep(5)
            continue
        if not delivered_or_claimed:
            await asyncio.sleep(2)
