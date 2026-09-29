"""Durable delivery of user submissions to staff chats.

Delivery must not depend on any single Telegram API being usable: a chat may have
topics disabled, the bot may temporarily lack "manage topics", the network can
drop between two calls, or Telegram can answer 429. Applications, tickets and
suggestions are promises made to a user, so this module makes sure they survive:

* a forum topic is created when it works and a plain chat message is used when it
  does not, so a chat without topics still receives everything;
* the pending delivery is stored in ``outbox_events`` in the same transaction that
  stores the item, so a crash between "saved" and "sent" cannot lose it;
* failed attempts are retried with a capped exponential backoff (see
  :mod:`mellow.middleware`) instead of being dropped, and the author is told when a
  delayed delivery finally succeeds.

Delivery is at-least-once: an attempt that Telegram accepted but that could not be
recorded is repeated. A repeated notification is better than a lost application.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mellow.config import Settings
from mellow.keyboards import application_review, ticket_actions
from mellow.models import Application, OutboxEvent, Suggestion, Ticket, User, utcnow
from mellow.rendering import (application_staff_text, application_topic_name,
                              service_item_staff_text, service_item_topic_name)

log = logging.getLogger("mellow.delivery")

APPLICATION_EVENT_TYPE = "application_delivery"
SERVICE_EVENT_TYPE = "ticket_delivery"

# A chat that refused a topic is not asked again immediately: a missing right or a
# disabled "Topics" setting is usually fixed by a human, not by a retry loop.
TOPIC_RETRY_SECONDS = 900
_topics_unavailable: dict[int, float] = {}

# Telegram answers a send with one of these when the topic no longer exists.
_DEAD_THREAD_MARKERS = ("message thread not found", "topic was deleted", "topic_deleted",
                        "thread not found", "topic_closed", "topic closed", "topic_id_invalid")


def safe_error(exc: BaseException) -> str:
    """Describe an error for logs without ever including message content."""
    description = getattr(exc, "message", None)
    if description:
        return f"{type(exc).__name__}: {description}"[:200]
    return type(exc).__name__


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of one delivery attempt. ``error`` carries a log-safe description."""

    delivered: bool
    thread_id: int | None = None
    error: str | None = None


def application_event_key(application_id: int) -> str:
    return f"application-delivery-{application_id}"


def service_event_key(kind: str, item_id: int) -> str:
    return f"{kind}-delivery-{item_id}"


def remember_topic_failure(chat_id: int) -> None:
    _topics_unavailable[chat_id] = time.monotonic()


def reset_topic_cache() -> None:
    """Forget which chats refused a topic (used by tests and after a rights change)."""
    _topics_unavailable.clear()


def topics_are_known_unavailable(chat_id: int) -> bool:
    recorded = _topics_unavailable.get(chat_id)
    if recorded is None:
        return False
    if time.monotonic() - recorded >= TOPIC_RETRY_SECONDS:
        del _topics_unavailable[chat_id]
        return False
    return True


async def resolve_thread_id(bot: Bot, chat_id: int, title: str) -> int | None:
    """Create a forum topic, or return ``None`` when the chat cannot have one.

    Returning ``None`` is not a failure: the item is then posted to the chat root,
    which is exactly what a chat without topics expects. Every failure of
    ``createForumTopic`` is treated as "no topics here" — the plain send that
    follows is the authoritative test of whether the destination is reachable.
    """
    if topics_are_known_unavailable(chat_id):
        return None
    try:
        topic = await bot.create_forum_topic(chat_id=chat_id, name=title)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        remember_topic_failure(chat_id)
        log.warning("Topics are unavailable in chat %s, posting to the chat root instead: %s",
                    chat_id, safe_error(exc))
        return None
    return topic.message_thread_id


async def send_to_staff_chat(bot: Bot, chat_id: int, text: str, *, thread_id: int | None = None,
                             reply_markup=None) -> tuple[int, int | None]:
    """Send one message, dropping a dead topic instead of losing the message.

    Returns the message id and the thread that was actually used (``None`` when the
    message went to the chat root).
    """
    if thread_id is not None:
        try:
            message = await bot.send_message(chat_id=chat_id, text=text, message_thread_id=thread_id,
                                             reply_markup=reply_markup, parse_mode="HTML")
            return message.message_id, thread_id
        except TelegramBadRequest as exc:
            if not any(marker in str(exc).lower() for marker in _DEAD_THREAD_MARKERS):
                raise
            log.warning("Topic %s is gone, posting to the chat root instead: %s", thread_id, safe_error(exc))
    message = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode="HTML")
    return message.message_id, None


async def _mark_event_sent(session: AsyncSession, event_key: str) -> None:
    await session.execute(update(OutboxEvent)
                          .where(OutboxEvent.event_key == event_key, OutboxEvent.status != "sent")
                          .values(status="sent", last_error=None, updated_at=utcnow()))


async def _store_route(session_factory, model, item_id: int, chat_id: int, thread_id: int | None) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(update(model).where(model.id == item_id)
                              .values(chat_id=chat_id, thread_id=thread_id))


async def deliver_application(bot: Bot, settings: Settings, session_factory: async_sessionmaker,
                              application_id: int, *, announce: bool = False) -> DeliveryResult:
    """Deliver one application exactly once per successful attempt.

    ``announce=True`` additionally notifies the author by direct message, which is
    what the retry worker does when a form was accepted but not delivered right away.
    """
    async with session_factory() as session:
        application = await session.get(Application, application_id)
        if application is None:
            return DeliveryResult(True)
        if application.status != "creating":
            return DeliveryResult(True, application.thread_id)
        owner = await session.get(User, application.user_id)
        data = dict(application.application_data or {})
        chat_id = application.chat_id or settings.applications_chat_id
        thread_id = application.thread_id
        username = owner.username if owner else None
        telegram_id = owner.telegram_id if owner else None

    if thread_id is None:
        thread_id = await resolve_thread_id(bot, chat_id, application_topic_name(application_id, data))
        if thread_id is not None:
            # Persist the topic before sending: a retry must reuse it, not create a twin.
            await _store_route(session_factory, Application, application_id, chat_id, thread_id)

    text = application_staff_text(application_id, data, settings, username, telegram_id or 0)
    try:
        _, used_thread = await send_to_staff_chat(bot, chat_id, text, thread_id=thread_id,
                                                  reply_markup=application_review(application_id))
    except Exception as exc:
        log.warning("Application #%s was not delivered: %s", application_id, safe_error(exc))
        return DeliveryResult(False, thread_id, safe_error(exc))

    async with session_factory() as session, session.begin():
        await session.execute(update(Application)
                              .where(Application.id == application_id, Application.status == "creating")
                              .values(status="pending", chat_id=chat_id, thread_id=used_thread, updated_at=utcnow()))
        await _mark_event_sent(session, application_event_key(application_id))
    if announce:
        await announce_delivery(bot, telegram_id, f"📬 Заявка #{application_id} отправлена администрации. "
                                                  "Мы сообщим о решении здесь.")
    return DeliveryResult(True, used_thread)


async def deliver_service_item(bot: Bot, settings: Settings, session_factory: async_sessionmaker,
                               kind: str, item_id: int, *, announce: bool = False) -> DeliveryResult:
    """Deliver one ticket or suggestion to its staff chat."""
    model = Suggestion if kind == "suggestion" else Ticket
    async with session_factory() as session:
        item = await session.get(model, item_id)
        if item is None:
            return DeliveryResult(True)
        if item.chat_id is not None:
            return DeliveryResult(True, item.thread_id)
        owner = await session.get(User, item.user_id)
        username = owner.username if owner else None
        telegram_id = owner.telegram_id if owner else None
        subject = getattr(item, "subject", "") or "Обращение"
        body = item.body
        destination = settings.suggestions_destination if kind == "suggestion" else settings.support_destination
        thread_id = item.thread_id

    if thread_id is None:
        thread_id = await resolve_thread_id(bot, destination, service_item_topic_name(subject, item_id))
        if thread_id is not None:
            await _store_route(session_factory, model, item_id, destination, thread_id)

    text = service_item_staff_text(kind, item_id, subject, body, username, telegram_id or 0)
    status = "new" if kind == "suggestion" else "open"
    try:
        _, used_thread = await send_to_staff_chat(bot, destination, text, thread_id=thread_id,
                                                  reply_markup=ticket_actions(kind, item_id, status))
    except Exception as exc:
        log.warning("%s #%s was not delivered: %s", kind, item_id, safe_error(exc))
        return DeliveryResult(False, thread_id, safe_error(exc))

    async with session_factory() as session, session.begin():
        await session.execute(update(model).where(model.id == item_id, model.chat_id.is_(None))
                              .values(chat_id=destination, thread_id=used_thread))
        await _mark_event_sent(session, service_event_key(kind, item_id))
    if announce:
        label = "Предложение" if kind == "suggestion" else "Обращение"
        await announce_delivery(bot, telegram_id, f"📬 {label} #{item_id} отправлено администрации.")
    return DeliveryResult(True, used_thread)


async def announce_delivery(bot: Bot, telegram_id: int | None, text: str) -> None:
    """Best-effort direct message; a blocked bot must never break the worker."""
    if not telegram_id:
        return
    try:
        await bot.send_message(telegram_id, text)
    except Exception as exc:
        log.info("Could not notify user about a delayed delivery: %s", safe_error(exc))


async def _ensure_event(session: AsyncSession, event_key: str, event_type: str, payload: dict) -> int:
    if await session.scalar(select(OutboxEvent.id).where(OutboxEvent.event_key == event_key)) is not None:
        return 0
    try:
        async with session.begin_nested():
            session.add(OutboxEvent(event_key=event_key, event_type=event_type, payload=payload, status="pending"))
            await session.flush()
    except IntegrityError:
        return 0
    return 1


async def reconcile_deliveries(session_factory: async_sessionmaker) -> int:
    """Queue deliveries that have no durable event yet.

    This heals two cases: rows written by an older build that created a topic but
    never confirmed the message, and any row whose event was lost with a crashed
    transaction. It runs from the outbox worker, so nothing has to be re-submitted
    by the user.
    """
    queued = 0
    async with session_factory() as session, session.begin():
        application_ids = list((await session.scalars(
            select(Application.id).where(Application.status == "creating"))).all())
        for application_id in application_ids:
            queued += await _ensure_event(session, application_event_key(application_id),
                                          APPLICATION_EVENT_TYPE, {"application_id": application_id})
        for kind, model, statuses in (("ticket", Ticket, ("open", "review")),
                                      ("suggestion", Suggestion, ("new", "review"))):
            item_ids = list((await session.scalars(
                select(model.id).where(model.chat_id.is_(None), model.status.in_(statuses)))).all())
            for item_id in item_ids:
                queued += await _ensure_event(session, service_event_key(kind, item_id),
                                              SERVICE_EVENT_TYPE, {"kind": kind, "id": item_id})
        revived = await session.execute(update(OutboxEvent)
                                        .where(OutboxEvent.event_type.in_((APPLICATION_EVENT_TYPE, SERVICE_EVENT_TYPE)),
                                               OutboxEvent.status == "failed")
                                        .values(status="pending", updated_at=utcnow()))
        queued += revived.rowcount or 0
    return queued
