"""The chat statistics command answers two questions: written and remaining."""
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mellow.config import Level, Settings
from mellow.models import Base, MessageStat, User, utcnow
from mellow.stats import (community_statistics, member_progress, render_community_statistics,
                          render_member_progress)


def settings(threshold: int = 10, enabled: bool = True) -> Settings:
    cfg = Settings(
        bot_token="test", database_url="sqlite+aiosqlite:///:memory:", applications_chat_id=-1,
        support_chat_id=None, suggestions_chat_id=None, administration_chat_id=-2,
        message_threshold=threshold, message_requirement_enabled=enabled,
        stats_top_limit=3, questions=[],
        levels={5: Level("Владелец", frozenset({"*"}), 0)},
    )
    return cfg


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield factory
    await engine.dispose()


async def add_member(factory, telegram_id: int, username: str | None, count: int, reached: bool = False):
    async with factory() as session, session.begin():
        user = User(telegram_id=telegram_id, username=username)
        session.add(user)
        await session.flush()
        session.add(MessageStat(user_id=user.id, message_count=count, first_message_at=utcnow(),
                                last_message_at=utcnow(), threshold_reached=reached))


async def test_chat_statistics_show_written_and_remaining_messages(db):
    cfg = settings(threshold=10)
    await add_member(db, 1, "active", 12, reached=True)
    await add_member(db, 2, "starter", 4)
    await add_member(db, 3, None, 1)
    await add_member(db, 4, "fourth", 0)

    async with db() as session:
        stats = await community_statistics(session, cfg, limit=cfg.stats_top_limit)
        rendered = render_community_statistics(stats, cfg)

    assert stats.total_messages == 17
    assert stats.tracked_members == 4
    assert stats.threshold_reached == 1
    assert len(stats.top) == 3
    assert "Всего сообщений: <b>17</b>" in rendered
    assert "@active — 12 ✅ порог выполнен" in rendered
    assert "@starter — 4, осталось 6" in rendered
    assert "ID 3 — 1, осталось 9" in rendered
    assert "…и ещё участников: 1" in rendered
    assert "Порог (10) выполнили: <b>1</b>" in rendered


async def test_personal_statistics_report_what_is_left(db):
    cfg = settings(threshold=10)
    await add_member(db, 42, "member", 7)
    async with db() as session:
        progress = await member_progress(session, cfg, 42)
        rendered = render_member_progress(progress, cfg)
    assert progress.message_count == 7
    assert progress.remaining == 3
    assert "Осталось написать: <b>3</b>" in rendered


async def test_threshold_is_not_counted_as_negative_remaining(db):
    cfg = settings(threshold=10)
    async with db() as session:
        progress = await member_progress(session, cfg, 999)
    assert (progress.message_count, progress.remaining, progress.threshold_reached) == (0, 10, False)

    cfg = settings(threshold=5)
    await add_member(db, 1000, "hero", 9, reached=True)
    async with db() as session:
        progress = await member_progress(session, cfg, 1000)
    assert progress.remaining == 0
    assert progress.threshold_reached is True
    assert "Порог выполнен ✅" in render_member_progress(progress, cfg)


async def test_disabled_counter_is_explained_instead_of_showing_zeros(db):
    cfg = settings(enabled=False)
    await add_member(db, 5, "member", 3)
    async with db() as session:
        stats = await community_statistics(session, cfg)
        rendered = render_community_statistics(stats, cfg)
    assert "Сбор статистики сообщений отключён" in rendered
