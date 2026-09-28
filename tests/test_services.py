from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mellow.config import Level, Settings
from mellow.models import Application, Base, MessageStat, OutboxEvent, Staff, User, utcnow
from mellow.middleware import dispatch_outbox_once
from mellow.services import claim_update, hierarchy_allows, increment_message_count, parse_duration


def settings(threshold=2):
    return Settings(
        bot_token="test", database_url="sqlite+aiosqlite:///:memory:", applications_chat_id=-1,
        support_chat_id=None, suggestions_chat_id=None, administration_chat_id=-2,
        message_threshold=threshold,
        levels={1: Level("Junior", frozenset({"warn"}), 86400),
                5: Level("Owner", frozenset({"*"}), 0)},
    )


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_count_threshold_once_and_update_dedupe(db):
    cfg = settings()
    assert await increment_message_count(db, cfg, 1001, "player", 1) == (1, False, False)
    assert await increment_message_count(db, cfg, 1001, "player", 1) == (None, False, True)
    assert await increment_message_count(db, cfg, 1001, "player", 2) == (2, True, False)
    assert await increment_message_count(db, cfg, 1001, "renamed", 3) == (3, False, False)
    async with db() as session:
        stat = await session.scalar(select(MessageStat))
        user = await session.scalar(select(User).where(User.telegram_id == 1001))
        assert stat.message_count == 3
        assert stat.threshold_reached is True
        assert user.username == "renamed"
        outbox = await session.scalar(select(OutboxEvent))
        assert outbox.event_type == "message_threshold"
        assert outbox.status == "pending"


@pytest.mark.asyncio
async def test_message_requirement_can_be_disabled(db):
    cfg = settings()
    cfg.message_requirement_enabled = False
    assert await increment_message_count(db, cfg, 888, "member", 80) == (None, False, False)
    async with db() as session:
        assert await session.scalar(select(MessageStat)) is None


@pytest.mark.asyncio
async def test_staff_messages_are_immune(db):
    cfg = settings()
    async with db() as session, session.begin():
        user = User(telegram_id=222, username="mod")
        session.add(user)
        await session.flush()
        session.add(Staff(user_id=user.id, level=1))
    assert await increment_message_count(db, cfg, 222, "mod", 20) == (None, False, False)
    async with db() as session, session.begin():
        user = await session.scalar(select(User).where(User.telegram_id == 222))
        staff = await session.get(Staff, user.id)
        staff.active = False
    assert await increment_message_count(db, cfg, 222, "mod", 21) == (1, False, False)
    async with db() as session:
        stat = await session.scalar(select(MessageStat))
        assert stat.message_count == 1


@pytest.mark.asyncio
async def test_update_claim_is_idempotent_without_user_association(db):
    assert await claim_update(db, 9001) is True
    assert await claim_update(db, 9001) is False
    assert await claim_update(db, 9002) is True


@pytest.mark.asyncio
async def test_outbox_sends_threshold_once(db):
    cfg = settings()
    async with db() as session, session.begin():
        user = User(telegram_id=555, username="member", minecraft_username="Steve123")
        session.add(user)
        await session.flush()
        session.add(MessageStat(user_id=user.id, message_count=2, first_message_at=utcnow(),
                                last_message_at=utcnow(), threshold_reached=True))
        session.add(OutboxEvent(event_key=f"message-threshold-{user.id}", event_type="message_threshold",
                                payload={"user_id": user.id}, status="pending"))
    bot = AsyncMock()
    assert await dispatch_outbox_once(bot, cfg, db) is True
    assert await dispatch_outbox_once(bot, cfg, db) is False
    bot.send_message.assert_awaited_once()
    async with db() as session:
        event = await session.scalar(select(OutboxEvent))
        assert event.status == "sent"


@pytest.mark.asyncio
async def test_one_active_application_per_user(db):
    from sqlalchemy.exc import IntegrityError
    async with db() as session, session.begin():
        user = User(telegram_id=444)
        session.add(user)
        await session.flush()
        session.add(Application(user_id=user.id, status="pending", application_data={}))
    async with db() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 444))
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(Application(user_id=user.id, status="creating", application_data={}))
                await session.flush()
    async with db() as session, session.begin():
        user = await session.scalar(select(User).where(User.telegram_id == 444))
        old = await session.scalar(select(Application).where(Application.user_id == user.id))
        old.status = "rejected"
        session.add(Application(user_id=user.id, status="creating", application_data={}))


def test_hierarchy_and_duration_rules():
    assert hierarchy_allows(2, 1)
    assert not hierarchy_allows(2, 2)
    assert not hierarchy_allows(2, 3)
    assert hierarchy_allows(5, 5)
    assert parse_duration("30м") == 1800
    assert parse_duration("2ч") == 7200
    assert parse_duration("7д") == 604800
    with pytest.raises(ValueError):
        parse_duration("forever")
