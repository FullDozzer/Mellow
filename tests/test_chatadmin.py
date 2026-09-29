"""Chat administration end-to-end: real updates through the real dispatcher.

These tests feed group messages exactly as Telegram would, so the command table, the
rank checks, the filters and the guard all run together. Telegram itself is replaced by a
recording session: no network, but the outgoing calls are real.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import (CallbackQuery, Chat, ChatMemberLeft, ChatMemberMember, ChatMemberUpdated, Message,
                           Sticker, Update, User as TelegramUser)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mellow.chatadmin.commands import router as chatadmin_router
from mellow.chatadmin.config import ChatSettingsStore
from mellow.chatadmin.guard import ChatGuard, RecentMessages
from mellow.config import Level, Settings
from mellow.handlers import router as main_router
from mellow.middleware import PrivacySafeMessageCounter
from mellow.models import Base, ChatMemberActivity, MessageStat, Punishment, Staff, Trigger, User, utcnow
from tests.conftest import attach_routers
from tests.test_update_flow import RecordingSession

GROUP_ID = -1001
OWNER_ID = 899
MODERATOR_ID = 900
MEMBER_ID = 901


def build_settings() -> Settings:
    return Settings(
        bot_token="42:TEST", database_url="sqlite+aiosqlite:///:memory:", applications_chat_id=-1001,
        support_chat_id=None, suggestions_chat_id=None, administration_chat_id=-1002,
        message_threshold=10,
        levels={1: Level("Младший модератор", frozenset({"warn", "mute", "applications", "tickets"}), 3600),
                2: Level("Модератор", frozenset({"warn", "mute", "ban", "kick", "unpunish"}), 604800),
                3: Level("Старший модератор", frozenset({"warn", "mute", "ban", "kick", "unpunish", "settings"}),
                         2592000),
                4: Level("Администратор", frozenset({"*"}), 31536000),
                5: Level("Владелец", frozenset({"*"}), 0)},
    )


class AdminSession(RecordingSession):
    """Adds the replies Telegram returns for moderation and membership calls."""

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        if name in {"GetChatMember", "GetChat"}:
            self.calls.append(method)
            return SimpleNamespace(status="member", can_send_messages=True, is_forum=False,
                                   id=getattr(method, "chat_id", 0), title="Mellow", username=None)
        if name == "GetMe":
            self.calls.append(method)
            return SimpleNamespace(id=42, is_bot=True, first_name="Mellow", username="mellow_bot")
        # Everything else goes to the recording session, which records it exactly once.
        return await super().make_request(bot, method, timeout)

    @property
    def deleted_messages(self) -> list[int]:
        ids = []
        for call in self.calls:
            if type(call).__name__ == "DeleteMessage":
                ids.append(call.message_id)
            elif type(call).__name__ == "DeleteMessages":
                ids.extend(call.message_ids)
        return ids

    @property
    def call_names(self) -> list[str]:
        return [type(call).__name__ for call in self.calls]


@pytest.fixture
async def app():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    cfg = build_settings()
    bot = Bot(token=cfg.bot_token, session=AdminSession())
    store = ChatSettingsStore(session_factory)
    recent = RecentMessages()
    dispatcher = Dispatcher()
    dispatcher["settings"] = cfg
    dispatcher["session_factory"] = session_factory
    dispatcher["store"] = store
    dispatcher["recent"] = recent
    dispatcher["minecraft"] = SimpleNamespace(add_to_whitelist=None)
    dispatcher.update.outer_middleware(PrivacySafeMessageCounter(cfg, session_factory))
    dispatcher.update.outer_middleware(ChatGuard(cfg, session_factory, store, recent))
    attach_routers(dispatcher, main_router, chatadmin_router)
    async with session_factory() as session, session.begin():
        owner = User(telegram_id=OWNER_ID, username="owner")
        session.add(owner)
        await session.flush()
        session.add(Staff(user_id=owner.id, level=5))
        moderator = User(telegram_id=MODERATOR_ID, username="mod")
        session.add(moderator)
        await session.flush()
        session.add(Staff(user_id=moderator.id, level=3))
        member = User(telegram_id=MEMBER_ID, username="player")
        session.add(member)
        await session.flush()
        session.add(MessageStat(user_id=member.id, message_count=4, first_message_at=utcnow(),
                                last_message_at=utcnow()))
    yield SimpleNamespace(bot=bot, dispatcher=dispatcher, session=bot.session, session_factory=session_factory,
                          settings=cfg, store=store)
    await bot.session.close()
    await engine.dispose()


def group_text(update_id: int, text: str, telegram_id: int = MODERATOR_ID, **extra) -> Update:
    return Update(update_id=update_id, message=Message(
        message_id=update_id, date=datetime.now(timezone.utc),
        chat=Chat(id=GROUP_ID, type="supergroup", title="Mellow", is_forum=False),
        from_user=TelegramUser(id=telegram_id, is_bot=False, first_name="Moderator"),
        text=text, **extra))


def group_sticker(update_id: int, telegram_id: int) -> Update:
    return Update(update_id=update_id, message=Message(
        message_id=update_id, date=datetime.now(timezone.utc),
        chat=Chat(id=GROUP_ID, type="supergroup", title="Mellow", is_forum=False),
        from_user=TelegramUser(id=telegram_id, is_bot=False, first_name="Member"),
        sticker=Sticker(file_id="s", file_unique_id="u", type="regular", width=1, height=1,
                        is_animated=False, is_video=False)))


async def last_reply(app) -> str:
    return app.session.sent_texts[-1]


async def punishments(app, ptype: str | None = None) -> list[Punishment]:
    async with app.session_factory() as session:
        statement = select(Punishment)
        if ptype:
            statement = statement.where(Punishment.type == ptype)
        return list((await session.scalars(statement)).all())


async def test_warn_command_records_a_warning_with_a_reason(app):
    await app.dispatcher.feed_update(app.bot, group_text(1, "варн @player Флуд в чате"))
    warns = await punishments(app, "warn")
    assert len(warns) == 1
    assert warns[0].reason == "Флуд в чате"
    assert warns[0].chat_id == GROUP_ID
    assert warns[0].target_user_id == MEMBER_ID
    assert warns[0].moderator_id == MODERATOR_ID
    assert "всего активных: 1 из 3" in await last_reply(app)


async def test_reason_can_be_written_on_the_next_line(app):
    await app.dispatcher.feed_update(app.bot, group_text(2, "варн @player\nСпам ссылками"))
    warns = await punishments(app, "warn")
    assert warns[0].reason == "Спам ссылками"


async def test_warning_limit_punishes_automatically(app):
    await app.store.update(GROUP_ID, warning_limit=2, warning_ban_seconds=7200)
    await app.dispatcher.feed_update(app.bot, group_text(3, "варн @player Первое"))
    await app.dispatcher.feed_update(app.bot, group_text(4, "варн @player Второе"))
    bans = await punishments(app, "ban")
    assert len(bans) == 1
    assert bans[0].duration == 7200
    assert "лимит предупреждений" in (await last_reply(app))


async def test_mute_period_is_parsed_as_words(app):
    await app.dispatcher.feed_update(app.bot, group_text(5, "мут 30 минут @player"))
    mutes = await punishments(app, "mute")
    assert mutes[0].duration == 1800
    assert "RestrictChatMember" in app.session.call_names


async def test_ban_is_limited_by_the_actor_level(app):
    await app.dispatcher.feed_update(app.bot, group_text(6, "бан навсегда @player"))
    bans = await punishments(app, "ban")
    # Level 3 may not ban forever: the action is capped by the level's maximum.
    assert bans[0].duration == app.settings.levels[3].max_punishment_seconds


async def test_member_cannot_use_moderation_commands(app):
    await app.dispatcher.feed_update(app.bot, group_text(7, "бан @player", telegram_id=MEMBER_ID))
    assert await punishments(app, "ban") == []


async def test_unwarn_and_rank_management(app):
    await app.dispatcher.feed_update(app.bot, group_text(8, "варн @player Флуд"))
    await app.dispatcher.feed_update(app.bot, group_text(9, "снятьварн @player"))
    warns = await punishments(app, "warn")
    assert all(not warn.active for warn in warns)

    await app.dispatcher.feed_update(app.bot, group_text(10, "мой ранг"))
    assert "3 —" in await last_reply(app)

    await app.dispatcher.feed_update(app.bot, group_text(11, "кто админ"))
    assert "@mod" in await last_reply(app)


async def test_command_access_can_be_restricted(app):
    await app.dispatcher.feed_update(app.bot, group_text(12, "дк варны 4", telegram_id=OWNER_ID))
    await app.dispatcher.feed_update(app.bot, group_text(13, "варн @player Слишком рано", telegram_id=MODERATOR_ID))
    assert await punishments(app, "warn") == []
    assert "Недостаточно прав" in await last_reply(app)

    await app.dispatcher.feed_update(app.bot, group_text(14, "дк сброс варны", telegram_id=OWNER_ID))
    await app.dispatcher.feed_update(app.bot, group_text(15, "варн @player Теперь можно"))
    assert len(await punishments(app, "warn")) == 1


async def test_link_filter_deletes_the_message_and_warns(app):
    await app.dispatcher.feed_update(app.bot, group_text(16, "-ссылки"))
    assert "запрещены" in await last_reply(app)
    app.session.calls.clear()
    await app.dispatcher.feed_update(app.bot, group_text(17, "заходите на https://spam.example тут всё",
                                                         telegram_id=MEMBER_ID))
    assert app.session.deleted_messages == [17]
    warns = await punishments(app, "warn")
    assert len(warns) == 1 and warns[0].chat_id == GROUP_ID
    assert "BanChatMember" not in app.session.call_names


async def test_moderator_messages_are_not_filtered(app):
    await app.dispatcher.feed_update(app.bot, group_text(18, "-ссылки"))
    app.session.calls.clear()
    await app.dispatcher.feed_update(app.bot, group_text(19, "https://ok.example", telegram_id=MODERATOR_ID))
    assert app.session.deleted_messages == []
    assert await punishments(app, "warn") == []


async def test_caps_filter_respects_the_configured_threshold(app):
    await app.dispatcher.feed_update(app.bot, group_text(20, "-капс 50 5"))
    app.session.calls.clear()
    await app.dispatcher.feed_update(app.bot, group_text(21, "ПРИВЕТ ВСЕМ КАК ДЕЛА", telegram_id=MEMBER_ID))
    assert app.session.deleted_messages == [21]


async def test_trigger_overrides_the_default_punishment(app):
    await app.dispatcher.feed_update(app.bot, group_text(22, "+триггер ссылки 1\nМут 10 минут / Реклама",
                                                         telegram_id=OWNER_ID))
    assert "Триггер установлен" in await last_reply(app)
    async with app.session_factory() as session:
        triggers = list((await session.scalars(select(Trigger))).all())
    assert triggers[0].event == "ссылки" and triggers[0].actions[0]["command"] == "мут"

    await app.dispatcher.feed_update(app.bot, group_text(23, "-ссылки"))
    app.session.calls.clear()
    await app.dispatcher.feed_update(app.bot, group_text(24, "t.me/+invite", telegram_id=MEMBER_ID))
    mutes = await punishments(app, "mute")
    assert len(mutes) == 1 and mutes[0].duration == 600
    assert mutes[0].reason == "Реклама"

    await app.dispatcher.feed_update(app.bot, group_text(25, "триггеры", telegram_id=OWNER_ID))
    assert "ссылки" in await last_reply(app)


async def test_sticker_limit_and_cleanup_confirmation(app):
    await app.dispatcher.feed_update(app.bot, group_text(26, "-стикеры 3"))
    await app.dispatcher.feed_update(app.bot, group_sticker(27, MEMBER_ID))
    await app.dispatcher.feed_update(app.bot, group_sticker(28, MEMBER_ID))
    assert await punishments(app, "warn") == []
    await app.dispatcher.feed_update(app.bot, group_sticker(29, MEMBER_ID))
    assert len(await punishments(app, "warn")) >= 1

    await app.dispatcher.feed_update(app.bot, group_text(30, "удалить 2"))
    reply = await last_reply(app)
    assert "Удалить" in reply
    token = reply and None
    from mellow.chatadmin.admin_commands import PENDING_CLEANUPS
    token = next(iter(PENDING_CLEANUPS))
    app.session.calls.clear()
    callback = CallbackQuery(id="cb1", from_user=TelegramUser(id=MODERATOR_ID, is_bot=False, first_name="Mod"),
                             chat_instance="1", data=f"cleanup:go:{token}",
                             message=Message(message_id=31, date=datetime.now(timezone.utc),
                                             chat=Chat(id=GROUP_ID, type="supergroup", title="Mellow"),
                                             from_user=TelegramUser(id=42, is_bot=True, first_name="Mellow"),
                                             text="Удалить 2 сообщений?"))
    await app.dispatcher.feed_update(app.bot, Update(update_id=32, callback_query=callback))
    assert "DeleteMessages" in app.session.call_names or "DeleteMessage" in app.session.call_names


async def test_chat_statistics_and_profile(app):
    await app.dispatcher.feed_update(app.bot, group_text(33, "чат инфо"))
    assert "Чат инфо" in await last_reply(app)

    await app.dispatcher.feed_update(app.bot, group_text(34, "чат стата 90"))
    reply = await last_reply(app)
    assert "Статистика чата" in reply and "<pre>" in reply and "за 90 дней" in reply

    await app.dispatcher.feed_update(app.bot, group_text(341, "чат стата 7"))
    assert "от 30 до 5000" in await last_reply(app)

    await app.dispatcher.feed_update(app.bot, group_text(342, "чат стата"))
    assert "за 365 дней" in await last_reply(app)

    await app.dispatcher.feed_update(app.bot, group_text(35, "стата"))
    assert "Статистика сообщений" in await last_reply(app)

    await app.dispatcher.feed_update(app.bot, group_text(36, "моя стата", telegram_id=MEMBER_ID))
    assert "Твоя статистика" not in await last_reply(app)  # the chat layer prints a profile card
    assert "Сообщений в счёте: 5" in await last_reply(app)

    await app.dispatcher.feed_update(app.bot, group_text(37, "профиль @player"))
    assert "@player" in await last_reply(app)


async def test_grid_links_chats_and_lists_them(app):
    await app.dispatcher.feed_update(app.bot, group_text(38, "дк установить сетку Тестовая", telegram_id=OWNER_ID))
    assert "сетку" in await last_reply(app)
    await app.dispatcher.feed_update(app.bot, group_text(39, "чаты", telegram_id=OWNER_ID))
    reply = await last_reply(app)
    assert "Тестовая" in reply and "Mellow" in reply


async def test_settings_are_visible_and_mutable(app):
    await app.dispatcher.feed_update(app.bot, group_text(40, "-маты"))
    await app.dispatcher.feed_update(app.bot, group_text(41, "варны лимит 5"))
    await app.dispatcher.feed_update(app.bot, group_text(42, "настройки чата"))
    reply = await last_reply(app)
    assert "Лимит предупреждений: 5" in reply
    assert "Фильтр сквернословия: включён" in reply

    await app.dispatcher.feed_update(app.bot, group_text(43, "приветствие Добро пожаловать"))
    await app.dispatcher.feed_update(app.bot, group_text(44, "+график"))
    config = await app.store.get(GROUP_ID)
    assert config.welcome_text == "Добро пожаловать" and config.show_charts is True


async def test_profanity_filter_catches_obfuscation(app):
    await app.dispatcher.feed_update(app.bot, group_text(45, "-маты"))
    app.session.calls.clear()
    await app.dispatcher.feed_update(app.bot, group_text(46, "ты бл*ть совсем", telegram_id=MEMBER_ID))
    assert app.session.deleted_messages == [46]


async def test_join_is_tracked_and_greeted(app):
    await app.dispatcher.feed_update(app.bot, group_text(47, "приветствие Здравствуй, путник"))
    newcomer = TelegramUser(id=777, is_bot=False, first_name="Newcomer")
    update = Update(update_id=48, message=Message(
        message_id=48, date=datetime.now(timezone.utc),
        chat=Chat(id=GROUP_ID, type="supergroup", title="Mellow"),
        from_user=newcomer, new_chat_members=[newcomer]))
    app.session.calls.clear()
    await app.dispatcher.feed_update(app.bot, update)
    assert any("Здравствуй, путник" in text for text in app.session.sent_texts)
    async with app.session_factory() as session:
        row = await session.get(ChatMemberActivity, (GROUP_ID, 777))
    assert row is not None and row.is_member is True


async def test_left_member_is_marked(app):
    member = TelegramUser(id=MEMBER_ID, is_bot=False, first_name="Player")
    update = Update(update_id=49, message=Message(
        message_id=49, date=datetime.now(timezone.utc),
        chat=Chat(id=GROUP_ID, type="supergroup", title="Mellow"),
        from_user=member, left_chat_member=member))
    await app.dispatcher.feed_update(app.bot, update)
    async with app.session_factory() as session:
        row = await session.get(ChatMemberActivity, (GROUP_ID, MEMBER_ID))
    assert row is not None and row.is_member is False


async def test_kick_inactive_previews_targets(app):
    async with app.session_factory() as session, session.begin():
        session.add(ChatMemberActivity(chat_id=GROUP_ID, telegram_id=555, joined_at=utcnow(),
                                       last_message_at=utcnow() - timedelta(days=90), is_member=True))
    await app.dispatcher.feed_update(app.bot, group_text(50, "кик неактив 30 дней"))
    reply = await last_reply(app)
    assert "555" in reply and "Подтвердить?" in reply


async def test_chat_member_update_tracks_membership(app):
    update = Update(update_id=51, chat_member=ChatMemberUpdated(
        chat=Chat(id=GROUP_ID, type="supergroup", title="Mellow"),
        from_user=TelegramUser(id=MODERATOR_ID, is_bot=False, first_name="Mod"),
        date=datetime.now(timezone.utc),
        old_chat_member=ChatMemberLeft(user=TelegramUser(id=888, is_bot=False, first_name="Joiner")),
        new_chat_member=ChatMemberMember(user=TelegramUser(id=888, is_bot=False, first_name="Joiner"),
                                         status="member")))
    await app.dispatcher.feed_update(app.bot, update)
    async with app.session_factory() as session:
        row = await session.get(ChatMemberActivity, (GROUP_ID, 888))
    assert row is not None and row.is_member is True
