from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aiogram import BaseMiddleware, Bot
from aiogram.types import Update
from mellow.config import Settings
from mellow.delivery import (APPLICATION_EVENT_TYPE, SERVICE_EVENT_TYPE, deliver_application,
                             deliver_service_item, reconcile_deliveries, safe_error)
from mellow.keyboards import threshold_actions
from mellow.models import MessageStat, OutboxEvent, User, utcnow
from mellow.privacy import is_anonymous_or_group_authored
from mellow.services import claim_update, increment_message_count
from sqlalchemy import select, update

log = logging.getLogger("mellow.stats")

# Events that are a user's only copy of what they submitted: they are retried until
# they are delivered. Threshold notifications keep the historical "one attempt, then
# manual review" behaviour, because a duplicate could be mistaken for a new event.
RETRYABLE_EVENT_TYPES = frozenset({APPLICATION_EVENT_TYPE, SERVICE_EVENT_TYPE})
RETRY_BASE_SECONDS = 15
RETRY_MAX_SECONDS = 6 * 60 * 60
STALE_CLAIM_SECONDS = 300
RECONCILE_INTERVAL_SECONDS = 30


class UnsupportedOutboxEvent(RuntimeError):
    pass


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


def retry_delay(attempts: int) -> int:
    """Capped exponential backoff: 15s, 30s, … and never longer than 6 hours."""
    exponent = max(0, min(attempts - 1, 12))
    return min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** exponent))


def is_due(payload: dict, now) -> bool:
    raw = (payload or {}).get("next_attempt_at")
    if not raw:
        return True
    try:
        scheduled = __import__("datetime").datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return True
    if scheduled.tzinfo is None:
        return True
    return scheduled <= now


async def requeue_stale_claims(session_factory, stale_after_seconds: int = STALE_CLAIM_SECONDS) -> int:
    """Return events whose process died mid-send to the queue.

    Delivery is at-least-once on purpose: a repeated notification is a small price for
    never losing an application that a user already sent.
    """
    cutoff = utcnow() - timedelta(seconds=stale_after_seconds)
    async with session_factory() as session, session.begin():
        result = await session.execute(update(OutboxEvent)
                                       .where(OutboxEvent.status == "sending", OutboxEvent.updated_at < cutoff)
                                       .values(status="pending", updated_at=utcnow()))
        return result.rowcount or 0


async def _finish_event(session_factory, event_id: int, status: str, error: str | None) -> None:
    async with session_factory() as session, session.begin():
        event = await session.get(OutboxEvent, event_id)
        if event is not None:
            event.status = status
            event.last_error = error[:120] if error else None
            event.updated_at = utcnow()


async def _reschedule_event(session_factory, event_id: int, error: str | None) -> None:
    async with session_factory() as session, session.begin():
        event = await session.get(OutboxEvent, event_id)
        if event is None:
            return
        payload = dict(event.payload or {})
        attempts = int(payload.get("attempts") or 0) + 1
        payload["attempts"] = attempts
        payload["next_attempt_at"] = (utcnow() + timedelta(seconds=retry_delay(attempts))).isoformat()
        event.payload = payload
        event.status = "pending"
        event.last_error = (error or "unknown")[:120]
        event.updated_at = utcnow()
        if attempts <= 5 or attempts % 20 == 0:
            log.warning("Delivery event %s postponed (attempt %s): %s", event_id, attempts, event.last_error)


async def _deliver_threshold_event(bot: Bot, settings: Settings, session_factory, payload: dict) -> None:
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


async def _deliver_event(bot: Bot, settings: Settings, session_factory, event_type: str, payload: dict) -> None:
    if event_type == "message_threshold":
        await _deliver_threshold_event(bot, settings, session_factory, payload)
        return
    if event_type == APPLICATION_EVENT_TYPE:
        result = await deliver_application(bot, settings, session_factory,
                                           int(payload["application_id"]), announce=True)
        if not result.delivered:
            raise RuntimeError(result.error or "application was not delivered")
        return
    if event_type == SERVICE_EVENT_TYPE:
        result = await deliver_service_item(bot, settings, session_factory,
                                            str(payload.get("kind", "ticket")), int(payload["id"]), announce=True)
        if not result.delivered:
            raise RuntimeError(result.error or "service item was not delivered")
        return
    raise UnsupportedOutboxEvent(f"unsupported outbox event: {event_type}")


async def dispatch_outbox_once(bot: Bot, settings: Settings, session_factory) -> bool:
    """Claim and deliver one due durable event; ``False`` when nothing is due."""
    await requeue_stale_claims(session_factory)
    now = utcnow()
    async with session_factory() as session, session.begin():
        candidates = list((await session.scalars(select(OutboxEvent).where(OutboxEvent.status == "pending")
                                                 .order_by(OutboxEvent.id).limit(200))).all())
        event = next((item for item in candidates if is_due(item.payload or {}, now)), None)
        if event is None:
            return False
        event.status, event.updated_at = "sending", now
        event_id, event_type, payload = event.id, event.event_type, dict(event.payload or {})
    retryable = event_type in RETRYABLE_EVENT_TYPES
    try:
        await _deliver_event(bot, settings, session_factory, event_type, payload)
    except UnsupportedOutboxEvent as exc:
        await _finish_event(session_factory, event_id, "failed", str(exc))
        log.error("Unsupported outbox event, event_id=%s", event_id)
        return True
    except Exception as exc:
        error = safe_error(exc)
        if retryable:
            await _reschedule_event(session_factory, event_id, error)
        else:
            await _finish_event(session_factory, event_id, "failed", error)
            log.warning("Outbox delivery failed, event_id=%s error=%s", event_id, error)
        return True
    await _finish_event(session_factory, event_id, "sent", None)
    return True


async def outbox_worker(bot: Bot, settings: Settings, session_factory):
    """Deliver queued events and keep re-queueing anything that was never delivered."""
    next_reconcile = 0.0
    while True:
        loop_time = asyncio.get_running_loop().time()
        if loop_time >= next_reconcile:
            try:
                queued = await reconcile_deliveries(session_factory)
                if queued:
                    log.info("Queued %s undelivered item(s) for delivery", queued)
            except Exception:
                log.exception("Delivery reconciliation failed")
            next_reconcile = loop_time + RECONCILE_INTERVAL_SECONDS
        try:
            processed = await dispatch_outbox_once(bot, settings, session_factory)
        except Exception:
            log.exception("Outbox worker could not process an event")
            await asyncio.sleep(5)
            continue
        if not processed:
            await asyncio.sleep(2)
