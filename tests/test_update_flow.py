"""End-to-end wiring: real Telegram updates through the real dispatcher.

The other test modules call handler functions directly; this one feeds updates through
the same router and middleware that ``mellow.main`` builds, which is the only way to see
whether a command is actually reachable by a user. Aiogram allows a router to be attached
to a single dispatcher, so the whole journey lives in one test.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import Chat, Message, Update, User as TelegramUser
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mellow.chatadmin.commands import router as chatadmin_router
from mellow.chatadmin.config import ChatSettingsStore
from mellow.chatadmin.guard import RecentMessages
from mellow.config import Level, Question, Settings
from mellow.handlers import router
from mellow.middleware import PrivacySafeMessageCounter
from mellow.models import ApplicationDraft, Base, MessageStat, Staff, User, utcnow
from tests.conftest import attach_routers

QUESTIONS = [Question("minecraft_username", "Никнейм Minecraft", 16), Question("age", "Возраст", 3)]


class RecordingSession(BaseSession):
    """A Telegram session that records outgoing methods instead of sending them."""

    def __init__(self):
        super().__init__()
        self.calls = []

    @property
    def sent_texts(self) -> list[str]:
        return [call.text for call in self.calls if isinstance(call, SendMessage)]

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(message_id=len(self.calls), date=datetime.now(timezone.utc),
                           chat=Chat(id=method.chat_id, type="private"), text=method.text or "")
        if isinstance(method, AnswerCallbackQuery):
            return True
        return SimpleNamespace(message_thread_id=1, message_id=len(self.calls))

    async def stream_content(self, *args, **kwargs):  # pragma: no cover - not used
        yield b""

    async def close(self):
        return None


def build_settings() -> Settings:
    return Settings(
        bot_token="42:TEST", database_url="sqlite+aiosqlite:///:memory:", applications_chat_id=-1001,
        support_chat_id=None, suggestions_chat_id=None, administration_chat_id=-1002,
        message_threshold=10, questions=QUESTIONS,
        levels={1: Level("Младший модератор", frozenset({"warn", "applications"}), 86400),
                5: Level("Владелец", frozenset({"*"}), 0)},
    )


def private_message(update_id: int, text: str, telegram_id: int = 500) -> Update:
    return Update(update_id=update_id, message=Message(
        message_id=update_id, date=datetime.now(timezone.utc),
        chat=Chat(id=telegram_id, type="private"),
        from_user=TelegramUser(id=telegram_id, is_bot=False, first_name="Member"), text=text))


def group_message(update_id: int, text: str, chat_id: int = -1001, telegram_id: int = 900) -> Update:
    return Update(update_id=update_id, message=Message(
        message_id=update_id, date=datetime.now(timezone.utc),
        chat=Chat(id=chat_id, type="supergroup", title="Mellow"),
        from_user=TelegramUser(id=telegram_id, is_bot=False, first_name="Member"), text=text))


@pytest.fixture
async def app():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    cfg = build_settings()
    bot = Bot(token=cfg.bot_token, session=RecordingSession())
    dispatcher = Dispatcher()
    dispatcher["settings"] = cfg
    dispatcher["session_factory"] = session_factory
    dispatcher["minecraft"] = SimpleNamespace(add_to_whitelist=None)
    store = ChatSettingsStore(session_factory)
    dispatcher["store"] = store
    dispatcher["recent"] = RecentMessages()
    dispatcher.update.outer_middleware(PrivacySafeMessageCounter(cfg, session_factory))
    attach_routers(dispatcher, router, chatadmin_router)
    yield SimpleNamespace(bot=bot, dispatcher=dispatcher, session=bot.session,
                          session_factory=session_factory, settings=cfg, store=store)
    await bot.session.close()
    await engine.dispose()


async def test_user_journey_through_the_dispatcher(app):
    # 1. A member asks for statistics in a private chat and sees what is left.
    await app.dispatcher.feed_update(app.bot, private_message(1, "/статистика"))
    assert "Сообщений в чате: <b>0</b>" in app.session.sent_texts[0]
    assert "Осталось написать: <b>10</b>" in app.session.sent_texts[0]

    # 2. The statistics command works without a slash as well.
    await app.dispatcher.feed_update(app.bot, private_message(2, "Статистика"))
    assert "Осталось написать: <b>10</b>" in app.session.sent_texts[1]

    # 3. An unfinished draft must not swallow menu buttons or commands.
    async with app.session_factory() as session, session.begin():
        user = User(telegram_id=500)
        session.add(user)
        await session.flush()
        session.add(ApplicationDraft(user_id=user.id, data={}, question_index=0))
    await app.dispatcher.feed_update(app.bot, private_message(3, "🎮 Подать заявку"))
    assert "Заявка на вступление" in app.session.sent_texts[2]

    # 4. Everyone in a group sees the whole chat; staff have no personal line.
    async with app.session_factory() as session, session.begin():
        user = User(telegram_id=900, username="helper")
        session.add(user)
        await session.flush()
        session.add(Staff(user_id=user.id, level=5))
        member = User(telegram_id=901, username="active")
        session.add(member)
        await session.flush()
        session.add(MessageStat(user_id=member.id, message_count=4, first_message_at=utcnow(),
                                last_message_at=utcnow(), threshold_reached=False))
    await app.dispatcher.feed_update(app.bot, group_message(4, "статистика"))
    answer = app.session.sent_texts[3]
    assert "Всего сообщений: <b>4</b>" in answer
    assert "@active — 4, осталось 6" in answer
    assert "Порог (10) выполнили: <b>0</b>" in answer
    assert "Твоя статистика" not in answer

    # 5. A member in the same group sees the board and their own remaining count
    #    (their own message is already counted by the middleware before the handler runs).
    await app.dispatcher.feed_update(app.bot, group_message(5, "/статистика", telegram_id=902))
    answer = app.session.sent_texts[4]
    assert "Твоя статистика" in answer
    assert "Осталось написать: <b>9</b>" in answer
    assert "Всего сообщений: <b>5</b>" in answer
    assert "ID 902 — 1, осталось 9" in answer
