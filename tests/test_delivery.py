"""Delivery must survive a chat without topics, a crash and a flaky Telegram."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mellow.config import Level, Question, Settings
from mellow.delivery import (application_event_key, deliver_application, deliver_service_item,
                             reconcile_deliveries, reset_topic_cache)
from mellow.handlers import find_replied_ticket, submit_application
from mellow.middleware import dispatch_outbox_once, requeue_stale_claims
from mellow.models import (Application, ApplicationDraft, Base, OutboxEvent, Suggestion, Ticket, User, utcnow)

QUESTIONS = [Question("minecraft_username", "Никнейм Minecraft", 16),
             Question("age", "Возраст", 3),
             Question("motivation", "Почему ты хочешь присоединиться?", 1000)]


def settings() -> Settings:
    return Settings(
        bot_token="test", database_url="sqlite+aiosqlite:///:memory:", applications_chat_id=-1001,
        support_chat_id=-1002, suggestions_chat_id=-1003, administration_chat_id=-1004,
        message_threshold=10, questions=QUESTIONS,
        levels={1: Level("Младший модератор", frozenset({"applications"}), 86400),
                5: Level("Владелец", frozenset({"*"}), 0)},
    )


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    # A chat that once refused a topic is not asked again for a while; each test
    # needs a clean slate to be independent of the others.
    reset_topic_cache()
    yield factory
    reset_topic_cache()
    await engine.dispose()


def forum_error() -> TelegramBadRequest:
    return TelegramBadRequest(method=None, message="Bad Request: the chat is not a forum")


async def store_application(factory, *, with_event: bool = True, telegram_id: int = 501) -> int:
    async with factory() as session, session.begin():
        user = User(telegram_id=telegram_id, username="applicant")
        session.add(user)
        await session.flush()
        application = Application(user_id=user.id, status="creating",
                                  application_data={"minecraft_username": "Steve", "age": "20",
                                                    "motivation": "хочу играть"})
        session.add(application)
        await session.flush()
        if with_event:
            session.add(OutboxEvent(event_key=application_event_key(application.id),
                                    event_type="application_delivery",
                                    payload={"application_id": application.id}, status="pending"))
        return application.id


async def test_application_is_delivered_when_the_chat_has_no_topics(db):
    cfg = settings()
    application_id = await store_application(db)
    bot = AsyncMock()
    bot.create_forum_topic.side_effect = forum_error()
    bot.send_message.return_value = SimpleNamespace(message_id=7)

    result = await deliver_application(bot, cfg, db, application_id)

    assert result.delivered is True
    assert result.thread_id is None
    assert "message_thread_id" not in bot.send_message.await_args.kwargs
    assert bot.send_message.await_args.kwargs["chat_id"] == cfg.applications_chat_id
    async with db() as session:
        application = await session.get(Application, application_id)
        event = await session.scalar(select(OutboxEvent))
    assert (application.status, application.chat_id, application.thread_id) == ("pending", cfg.applications_chat_id, None)
    assert event.status == "sent"


async def test_application_uses_a_topic_when_topics_are_available(db):
    cfg = settings()
    application_id = await store_application(db)
    bot = AsyncMock()
    bot.create_forum_topic.return_value = SimpleNamespace(message_thread_id=42)
    bot.send_message.return_value = SimpleNamespace(message_id=8)

    result = await deliver_application(bot, cfg, db, application_id)

    assert result.delivered is True
    assert bot.send_message.await_args.kwargs["message_thread_id"] == 42
    async with db() as session:
        application = await session.get(Application, application_id)
    assert application.thread_id == 42


async def test_topic_is_created_before_the_message_so_a_retry_never_duplicates_it(db):
    cfg = settings()
    application_id = await store_application(db)
    bot = AsyncMock()
    bot.create_forum_topic.return_value = SimpleNamespace(message_thread_id=43)
    bot.send_message.side_effect = RuntimeError("network is down")

    result = await deliver_application(bot, cfg, db, application_id)

    assert result.delivered is False
    async with db() as session:
        application = await session.get(Application, application_id)
    assert (application.status, application.thread_id) == ("creating", 43)

    bot.send_message.side_effect = None
    bot.send_message.return_value = SimpleNamespace(message_id=9)
    assert (await deliver_application(bot, cfg, db, application_id)).delivered is True
    bot.create_forum_topic.assert_awaited_once()


async def test_failed_delivery_is_retried_until_it_succeeds_and_the_author_is_told(db, monkeypatch):
    cfg = settings()
    await store_application(db, telegram_id=777)
    monkeypatch.setattr("mellow.middleware.RETRY_BASE_SECONDS", 0)
    bot = AsyncMock()
    bot.create_forum_topic.side_effect = forum_error()
    bot.send_message.side_effect = RuntimeError("network is down")

    assert await dispatch_outbox_once(bot, cfg, db) is True
    async with db() as session:
        event = await session.scalar(select(OutboxEvent))
        application = await session.scalar(select(Application))
    assert event.status == "pending"
    assert (event.payload or {}).get("attempts") == 1
    assert application.status == "creating"

    bot.send_message.side_effect = None
    bot.send_message.return_value = SimpleNamespace(message_id=11)
    assert await dispatch_outbox_once(bot, cfg, db) is True
    async with db() as session:
        event = await session.scalar(select(OutboxEvent))
        application = await session.scalar(select(Application))
    assert event.status == "sent"
    assert application.status == "pending"
    notified = [call.args[0] if call.args else call.kwargs.get("chat_id")
                for call in bot.send_message.await_args_list]
    assert 777 in notified
    assert await dispatch_outbox_once(bot, cfg, db) is False


async def test_event_claimed_by_a_dead_process_is_returned_to_the_queue(db):
    cfg = settings()
    application_id = await store_application(db)
    async with db() as session, session.begin():
        event = await session.scalar(select(OutboxEvent))
        event.status = "sending"
        event.updated_at = utcnow() - timedelta(minutes=10)
    assert await requeue_stale_claims(db) == 1
    bot = AsyncMock()
    bot.create_forum_topic.side_effect = forum_error()
    bot.send_message.return_value = SimpleNamespace(message_id=12)
    assert await dispatch_outbox_once(bot, cfg, db) is True
    async with db() as session:
        application = await session.get(Application, application_id)
    assert application.status == "pending"


async def test_reconcile_queues_legacy_rows_without_an_event(db):
    await store_application(db, with_event=False)
    async with db() as session, session.begin():
        user = await session.scalar(select(User).where(User.telegram_id == 501))
        ticket = Ticket(user_id=user.id, type="support", subject="Проблема", body="Не заходит",
                        status="open")
        session.add(ticket)
        suggestion = Suggestion(user_id=user.id, body="Добавьте рыбалку", status="new", chat_id=-1001)
        session.add(suggestion)
    assert await reconcile_deliveries(db) == 2
    assert await reconcile_deliveries(db) == 0
    async with db() as session:
        keys = {event.event_key for event in (await session.scalars(select(OutboxEvent))).all()}
    assert "application-delivery-1" in keys
    assert any(key.startswith("ticket-delivery-") for key in keys)


async def test_ticket_is_delivered_without_topics_and_reuses_the_chat_root(db):
    cfg = settings()
    async with db() as session, session.begin():
        user = User(telegram_id=808, username="member")
        session.add(user)
        await session.flush()
        ticket = Ticket(user_id=user.id, type="support", subject="Проблема", body="Не заходит", status="open")
        session.add(ticket)
        await session.flush()
        ticket_id = ticket.id
    bot = AsyncMock()
    bot.create_forum_topic.side_effect = forum_error()
    bot.send_message.return_value = SimpleNamespace(message_id=13)

    result = await deliver_service_item(bot, cfg, db, "ticket", ticket_id)

    assert result.delivered is True
    assert bot.send_message.await_args.kwargs["chat_id"] == cfg.support_chat_id
    async with db() as session:
        ticket = await session.get(Ticket, ticket_id)
    assert (ticket.chat_id, ticket.thread_id) == (cfg.support_chat_id, None)


async def test_dead_topic_is_replaced_by_the_chat_root_instead_of_losing_the_message(db):
    cfg = settings()
    application_id = await store_application(db)
    async with db() as session, session.begin():
        application = await session.get(Application, application_id)
        application.chat_id, application.thread_id = cfg.applications_chat_id, 42
    bot = AsyncMock()
    bot.send_message.side_effect = [TelegramBadRequest(method=None, message="Bad Request: message thread not found"),
                                    SimpleNamespace(message_id=14)]

    result = await deliver_application(bot, cfg, db, application_id)

    assert result.delivered is True
    assert result.thread_id is None
    assert bot.send_message.await_count == 2
    async with db() as session:
        application = await session.get(Application, application_id)
    assert (application.status, application.thread_id) == ("pending", None)


async def test_staff_reply_without_topics_is_matched_by_the_bot_card(db):
    cfg = settings()
    async with db() as session, session.begin():
        user = User(telegram_id=808, username="member")
        session.add(user)
        await session.flush()
        ticket = Ticket(user_id=user.id, type="support", subject="Проблема", body="Не заходит",
                        status="open", chat_id=cfg.support_chat_id, thread_id=None)
        session.add(ticket)
        await session.flush()
        ticket_id = ticket.id
    card = SimpleNamespace(from_user=SimpleNamespace(is_bot=True),
                           text=f"Тикет #{ticket_id}\nАвтор: @member\nТема: Проблема\n\nНе заходит")
    human_reply = SimpleNamespace(from_user=SimpleNamespace(is_bot=False), text=card.text)

    assert (await find_replied_ticket(db, cfg.support_chat_id, None, card, bot=None)).id == ticket_id
    assert await find_replied_ticket(db, cfg.support_chat_id, None, human_reply, bot=None) is None


async def test_submit_application_reports_success_after_the_immediate_attempt(db):
    cfg = settings()
    callback = await prepare_draft(db, telegram_id=601)
    bot = AsyncMock()
    bot.create_forum_topic.return_value = SimpleNamespace(message_thread_id=44)
    bot.send_message.return_value = SimpleNamespace(message_id=15)

    await submit_application(callback, cfg, db, bot)

    message = callback.message.edit_text.await_args.args[0]
    assert "отправлена администрации" in message
    async with db() as session:
        application = await session.scalar(select(Application))
    assert application.status == "pending"


async def test_submit_application_never_loses_a_form_when_sending_fails(db):
    cfg = settings()
    callback = await prepare_draft(db, telegram_id=602)
    bot = AsyncMock()
    bot.create_forum_topic.side_effect = forum_error()
    bot.send_message.side_effect = RuntimeError("network is down")

    await submit_application(callback, cfg, db, bot)

    message = callback.message.edit_text.await_args.args[0]
    assert "автоматически" in message
    async with db() as session:
        application = await session.scalar(select(Application))
        event = await session.scalar(select(OutboxEvent))
    assert application.status == "creating"
    assert event.status == "pending"
    assert event.event_type == "application_delivery"


async def test_submit_application_asks_for_missing_answers_instead_of_dead_ending(db):
    cfg = settings()
    callback = await prepare_draft(db, telegram_id=603, data={"minecraft_username": "Steve"})
    bot = AsyncMock()

    await submit_application(callback, cfg, db, bot)

    asked = callback.message.answer.await_args.args[0]
    assert "Возраст" in asked
    async with db() as session:
        assert await session.scalar(select(Application)) is None
        draft = await session.scalar(select(ApplicationDraft))
    assert draft.question_index == 1


async def prepare_draft(factory, *, telegram_id: int, data: dict | None = None):
    """Store a fully answered draft and return a callback that presses "Отправить"."""
    async with factory() as session, session.begin():
        user = User(telegram_id=telegram_id, username="player")
        session.add(user)
        await session.flush()
        answers = data or {question.key: "ответ" for question in QUESTIONS}
        session.add(ApplicationDraft(user_id=user.id, data=answers, question_index=len(QUESTIONS)))
    callback = AsyncMock()
    callback.from_user = SimpleNamespace(id=telegram_id, username="player")
    callback.message = AsyncMock()
    callback.data = "app:submit"
    return callback
