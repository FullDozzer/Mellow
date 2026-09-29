"""The rest of the Iris sections: staff extras, access, cleanup, chat settings, анкета.

These tests reuse the end-to-end application from :mod:`tests.test_chatadmin`, so every
command is executed through the real dispatcher with the real command table.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram.types import (CallbackQuery, Chat, Message, Update, User as TelegramUser)
from sqlalchemy import select

from mellow.models import ChatMemberActivity, CreatorWill, Staff, User, utcnow
from tests.test_chatadmin import (GROUP_ID, MEMBER_ID, MODERATOR_ID, OWNER_ID, group_text,
                                  last_reply, punishments)
from tests.test_chatadmin import AdminSession, app as _chatadmin_app

# pytest discovers fixtures by the name they are bound to in the module.
app = _chatadmin_app


async def staff_level_of(app, telegram_id: int) -> int:
    async with app.session_factory() as session:
        row = await session.scalar(select(Staff.level).join(User, Staff.user_id == User.id)
                                   .where(User.telegram_id == telegram_id))
    return int(row or 0)


async def config_of(app):
    return await app.store.get(GROUP_ID)


def join_update(update_id: int, telegram_id: int, first_name: str = "Newcomer") -> Update:
    member = TelegramUser(id=telegram_id, is_bot=False, first_name=first_name)
    return Update(update_id=update_id, message=Message(
        message_id=update_id, date=datetime.now(timezone.utc),
        chat=Chat(id=GROUP_ID, type="supergroup", title="Mellow"),
        from_user=member, new_chat_members=[member]))


# --------------------------------------------------------------------------------------
# 1. Команды модерации
# --------------------------------------------------------------------------------------

async def test_staff_list_online_and_who_appointed(app):
    await app.dispatcher.feed_update(app.bot, group_text(60, "+мой онлайн", telegram_id=OWNER_ID))
    await app.dispatcher.feed_update(app.bot, group_text(61, "мой онлайн", telegram_id=OWNER_ID))
    assert "включено" in await last_reply(app)

    await app.dispatcher.feed_update(app.bot, group_text(62, "повысить @mod", telegram_id=OWNER_ID))
    assert await staff_level_of(app, MODERATOR_ID) == 4

    await app.dispatcher.feed_update(app.bot, group_text(63, "кто назначил @mod", telegram_id=OWNER_ID))
    reply = await last_reply(app)
    assert "выдал 4 ранг" in reply and f"@{'owner'}" in reply

    await app.dispatcher.feed_update(app.bot, group_text(64, "а судьи кто", telegram_id=OWNER_ID))
    assert "Администрация" in await last_reply(app)


async def test_summon_staff_and_delete_mentions(app):
    await app.dispatcher.feed_update(app.bot, group_text(65, "созвать модеров", telegram_id=OWNER_ID))
    reply = await last_reply(app)
    assert "Созыв модерации" in reply and "@mod" in reply and "@owner" not in reply

    callback = CallbackQuery(id="cb-summon", chat_instance="1", data="summon:delete",
                             from_user=TelegramUser(id=MODERATOR_ID, is_bot=False, first_name="Mod"),
                             message=Message(message_id=66, date=datetime.now(timezone.utc),
                                             chat=Chat(id=GROUP_ID, type="supergroup", title="Mellow"),
                                             from_user=TelegramUser(id=42, is_bot=True, first_name="Mellow"),
                                             text="Созыв модерации"))
    app.session.calls.clear()
    await app.dispatcher.feed_update(app.bot, Update(update_id=67, callback_query=callback))
    assert 66 in app.session.deleted_messages


async def test_creator_will_and_inheritance(app):
    await app.dispatcher.feed_update(app.bot, group_text(68, "+завещание @mod", telegram_id=OWNER_ID))
    assert "Завещание оставлено" in await last_reply(app)
    await app.dispatcher.feed_update(app.bot, group_text(69, "моё завещание", telegram_id=OWNER_ID))
    assert "@mod" in await last_reply(app)

    await app.dispatcher.feed_update(app.bot, group_text(70, "вступить в наследство @owner",
                                                         telegram_id=MODERATOR_ID))
    assert "права создателя" in await last_reply(app)
    assert await staff_level_of(app, MODERATOR_ID) == 5
    async with app.session_factory() as session:
        assert await session.get(CreatorWill, OWNER_ID) is None

    await app.dispatcher.feed_update(app.bot, group_text(71, "вступить в наследство @owner",
                                                         telegram_id=MEMBER_ID))
    assert "не оставлял" in await last_reply(app)


async def test_creator_title_is_transferred(app):
    await app.dispatcher.feed_update(app.bot, group_text(72, "передать создателя @mod",
                                                         telegram_id=OWNER_ID))
    assert "Права создателя переданы" in await last_reply(app)
    assert await staff_level_of(app, MODERATOR_ID) == 5
    assert await staff_level_of(app, OWNER_ID) == 4

    await app.dispatcher.feed_update(app.bot, group_text(73, "передать создателя @player",
                                                         telegram_id=OWNER_ID))
    assert "только создатель" in await last_reply(app)


# --------------------------------------------------------------------------------------
# 2. Баны и предупреждения: причина, тихий кик, амнистия, «остыть»
# --------------------------------------------------------------------------------------

async def test_reason_quiet_kick_and_amnesty(app):
    await app.dispatcher.feed_update(app.bot, group_text(74, "бан @player Спам", telegram_id=MODERATOR_ID))
    await app.dispatcher.feed_update(app.bot, group_text(75, "причина @player", telegram_id=MODERATOR_ID))
    reply = await last_reply(app)
    assert "Спам" in reply and "Модератор" in reply

    app.session.calls.clear()
    await app.dispatcher.feed_update(app.bot, group_text(76, "кик тихо @player", telegram_id=MODERATOR_ID))
    assert 76 in app.session.deleted_messages
    assert "BanChatMember" in app.session.call_names

    await app.dispatcher.feed_update(app.bot, group_text(77, "амнистия", telegram_id=MODERATOR_ID))
    assert "Амнистия" in await last_reply(app)
    assert [row for row in await punishments(app, "ban") if row.active] == []


async def test_cooldown_shortcuts(app):
    await app.dispatcher.feed_update(app.bot, group_text(78, "мут остыть @player", telegram_id=MODERATOR_ID))
    mutes = await punishments(app, "mute")
    assert len(mutes) == 1 and mutes[0].duration == 600

    await app.dispatcher.feed_update(app.bot, group_text(79, "варн остыть @player", telegram_id=MODERATOR_ID))
    warns = await punishments(app, "warn")
    assert len(warns) == 1 and warns[0].duration == 600


# --------------------------------------------------------------------------------------
# 4. Доступ команд
# --------------------------------------------------------------------------------------

async def test_command_can_be_disabled_and_restored(app):
    await app.dispatcher.feed_update(app.bot, group_text(80, "дк бан 6", telegram_id=OWNER_ID))
    assert "выключена" in await last_reply(app)

    await app.dispatcher.feed_update(app.bot, group_text(81, "бан @player Спам", telegram_id=MODERATOR_ID))
    assert "выключена" in await last_reply(app)
    assert await punishments(app, "ban") == []

    await app.dispatcher.feed_update(app.bot, group_text(82, "мой дк", telegram_id=MODERATOR_ID))
    assert "Мой доступ команд" in await last_reply(app)

    await app.dispatcher.feed_update(app.bot, group_text(83, "сброс команд", telegram_id=OWNER_ID))
    await app.dispatcher.feed_update(app.bot, group_text(84, "бан @player Спам", telegram_id=MODERATOR_ID))
    assert len(await punishments(app, "ban")) == 1


async def test_personal_access_and_notice_toggle(app):
    await app.dispatcher.feed_update(app.bot, group_text(85, "-лдк варн @mod", telegram_id=OWNER_ID))
    assert "закрыт" in await last_reply(app)
    await app.dispatcher.feed_update(app.bot, group_text(86, "варн @player", telegram_id=MODERATOR_ID))
    assert "Недостаточно прав" in await last_reply(app)
    assert await punishments(app, "warn") == []

    await app.dispatcher.feed_update(app.bot, group_text(87, "лдк @mod", telegram_id=OWNER_ID))
    assert "закрыт" in await last_reply(app)
    await app.dispatcher.feed_update(app.bot, group_text(88, "сброс лдк @mod", telegram_id=OWNER_ID))
    await app.dispatcher.feed_update(app.bot, group_text(89, "варн @player", telegram_id=MODERATOR_ID))
    assert len(await punishments(app, "warn")) == 1

    await app.dispatcher.feed_update(app.bot, group_text(90, "-команды", telegram_id=OWNER_ID))
    await app.dispatcher.feed_update(app.bot, group_text(91, "дк сброс всех лдк", telegram_id=OWNER_ID))
    await app.dispatcher.feed_update(app.bot, group_text(92, "дк варны 5", telegram_id=OWNER_ID))
    before = len(app.session.sent_texts)
    await app.dispatcher.feed_update(app.bot, group_text(93, "варн @player", telegram_id=MODERATOR_ID))
    assert len(app.session.sent_texts) == before  # оповещение выключено → молчание


async def test_access_log_and_import(app):
    await app.dispatcher.feed_update(app.bot, group_text(94, "дк варны 3", telegram_id=OWNER_ID))
    await app.dispatcher.feed_update(app.bot, group_text(95, "лог дк", telegram_id=OWNER_ID))
    assert "Лог доступа команд" in await last_reply(app)

    await app.dispatcher.feed_update(app.bot, group_text(96, "импорт команд из -1001", telegram_id=OWNER_ID))
    assert "тот же чат" in await last_reply(app)

    await app.dispatcher.feed_update(app.bot, group_text(97, "дк мдк 1", telegram_id=OWNER_ID))
    await app.dispatcher.feed_update(app.bot, group_text(98, "мой дк", telegram_id=MEMBER_ID))
    assert "доступна с 1 уровня" in await last_reply(app)  # «мой дк» теперь тоже настраивается

    await app.dispatcher.feed_update(app.bot, group_text(99, "дк вызов дк 6", telegram_id=OWNER_ID))
    await app.dispatcher.feed_update(app.bot, group_text(100, "дк", telegram_id=OWNER_ID))
    assert "выключена" in await last_reply(app)


# --------------------------------------------------------------------------------------
# 5. Чистка
# --------------------------------------------------------------------------------------

async def test_inactive_by_count_and_silent_deletion(app):
    async with app.session_factory() as session, session.begin():
        session.add(ChatMemberActivity(chat_id=GROUP_ID, telegram_id=501, joined_at=utcnow(),
                                       last_message_at=None, is_member=True))
        session.add(ChatMemberActivity(chat_id=GROUP_ID, telegram_id=502, joined_at=utcnow(),
                                       last_message_at=utcnow() - timedelta(days=5), is_member=True))
        session.add(ChatMemberActivity(chat_id=GROUP_ID, telegram_id=503, joined_at=utcnow(),
                                       last_message_at=utcnow(), is_member=True))
    await app.dispatcher.feed_update(app.bot, group_text(101, "кик неактив 2", telegram_id=MODERATOR_ID))
    reply = await last_reply(app)
    assert "501" in reply and "502" in reply and "503" not in reply

    app.session.calls.clear()
    await app.dispatcher.feed_update(app.bot, group_text(98, "-смс 3", telegram_id=MODERATOR_ID))
    assert app.session.deleted_messages


async def test_kick_silent_and_by_sms(app):
    async with app.session_factory() as session, session.begin():
        session.add(ChatMemberActivity(chat_id=GROUP_ID, telegram_id=511, joined_at=utcnow() - timedelta(days=40),
                                       last_message_at=None, is_member=True))
    await app.dispatcher.feed_update(app.bot, group_text(99, "кик молчунов 1", telegram_id=MODERATOR_ID))
    assert "Кик молчунов" in await last_reply(app)
    await app.dispatcher.feed_update(app.bot, group_text(100, "кик по смс 5 2 недели",
                                                         telegram_id=MODERATOR_ID))
    assert "Кик по смс" in await last_reply(app) or "никто не подходит" in await last_reply(app)


# --------------------------------------------------------------------------------------
# 6. Настройка чата
# --------------------------------------------------------------------------------------

async def test_chat_appearance_commands(app):
    app.session.calls.clear()
    await app.dispatcher.feed_update(app.bot, group_text(101, "название Mellow Town",
                                                         telegram_id=MODERATOR_ID))
    await app.dispatcher.feed_update(app.bot, group_text(102, "закреп 5", telegram_id=MODERATOR_ID))
    await app.dispatcher.feed_update(app.bot, group_text(103, "открепить", telegram_id=MODERATOR_ID))
    names = app.session.call_names
    assert "SetChatTitle" in names and "PinChatMessage" in names and "UnpinChatMessage" in names


async def test_channel_messages_can_be_forbidden(app):
    await app.dispatcher.feed_update(app.bot, group_text(104, "-каналы", telegram_id=MODERATOR_ID))
    assert (await config_of(app)).channels_denied is True

    from aiogram.types import Chat as TgChat
    update = Update(update_id=105, message=Message(
        message_id=105, date=datetime.now(timezone.utc),
        chat=TgChat(id=GROUP_ID, type="supergroup", title="Mellow"),
        sender_chat=TgChat(id=-1009, type="channel", title="Spam Channel"), text="реклама"))
    app.session.calls.clear()
    await app.dispatcher.feed_update(app.bot, update)
    assert 105 in app.session.deleted_messages
    assert "BanChatSenderChat" in app.session.call_names


async def test_join_and_leave_notices(app):
    await app.dispatcher.feed_update(app.bot, group_text(106, "+входы", telegram_id=MODERATOR_ID))
    await app.dispatcher.feed_update(app.bot, group_text(107, "+выходы", telegram_id=MODERATOR_ID))
    await app.dispatcher.feed_update(app.bot, join_update(108, 731))
    assert any("вход в чат" in text for text in app.session.sent_texts)

    left = TelegramUser(id=731, is_bot=False, first_name="Newcomer")
    update = Update(update_id=109, message=Message(
        message_id=109, date=datetime.now(timezone.utc),
        chat=Chat(id=GROUP_ID, type="supergroup", title="Mellow"),
        from_user=left, left_chat_member=left))
    await app.dispatcher.feed_update(app.bot, update)
    assert any("выход из чата" in text for text in app.session.sent_texts)


async def test_minreg_kicks_fresh_accounts(app):
    async with app.session_factory() as session, session.begin():
        session.add(User(telegram_id=741, username="fresh"))
    await app.dispatcher.feed_update(app.bot, group_text(110, "+минрег 1", telegram_id=MODERATOR_ID))
    await app.dispatcher.feed_update(app.bot, group_text(111, "минрег", telegram_id=MODERATOR_ID))
    assert "1 дн." in await last_reply(app)

    app.session.calls.clear()
    await app.dispatcher.feed_update(app.bot, join_update(112, 741, "Fresh"))
    assert "BanChatMember" in app.session.call_names


async def test_autokick_on_repeated_leaves(app):
    await app.dispatcher.feed_update(app.bot, group_text(113, "+автокик 2 60 бан",
                                                         telegram_id=MODERATOR_ID))
    assert "Автокик включён" in await last_reply(app)
    for update_id in (114, 115):
        left = TelegramUser(id=MEMBER_ID, is_bot=False, first_name="Player")
        update = Update(update_id=update_id, message=Message(
            message_id=update_id, date=datetime.now(timezone.utc),
            chat=Chat(id=GROUP_ID, type="supergroup", title="Mellow"),
            from_user=left, left_chat_member=left))
        await app.dispatcher.feed_update(app.bot, update)
    assert len(await punishments(app, "ban")) == 1


async def test_tags_and_membership_check(app):
    await app.dispatcher.feed_update(app.bot, group_text(116, "+тг тег олдфаг @player",
                                                         telegram_id=MODERATOR_ID))
    assert "олдфаг" in await last_reply(app)
    async with app.session_factory() as session:
        row = await session.get(ChatMemberActivity, (GROUP_ID, MEMBER_ID))
    assert row is not None and row.tag == "олдфаг"

    await app.dispatcher.feed_update(app.bot, group_text(117, "проверить в чате",
                                                         telegram_id=MODERATOR_ID))
    assert "Проверено участников" in await last_reply(app)

    await app.dispatcher.feed_update(app.bot, group_text(118, "-тг тег @player", telegram_id=MODERATOR_ID))
    async with app.session_factory() as session:
        row = await session.get(ChatMemberActivity, (GROUP_ID, MEMBER_ID))
    assert row.tag is None


async def test_join_requests_can_be_approved_automatically(app):
    await app.dispatcher.feed_update(app.bot, group_text(119, "+автозаявки", telegram_id=MODERATOR_ID))
    from aiogram.types import ChatJoinRequest

    request = ChatJoinRequest(chat=Chat(id=GROUP_ID, type="supergroup", title="Mellow"),
                              from_user=TelegramUser(id=751, is_bot=False, first_name="Asker"),
                              user_chat_id=751, date=datetime.now(timezone.utc))
    app.session.calls.clear()
    await app.dispatcher.feed_update(app.bot, Update(update_id=120, chat_join_request=request))
    assert "ApproveChatJoinRequest" in app.session.call_names
    async with app.session_factory() as session:
        row = await session.get(ChatMemberActivity, (GROUP_ID, 751))
    assert row is not None and row.is_member is True


# --------------------------------------------------------------------------------------
# 8 и 9. Анкета и статистика
# --------------------------------------------------------------------------------------

async def test_profile_is_available_as_a_form(app):
    await app.dispatcher.feed_update(app.bot, group_text(121, "анкета @player", telegram_id=MODERATOR_ID))
    reply = await last_reply(app)
    assert "Профиль" in reply and "Сообщений в счёте" in reply

    await app.dispatcher.feed_update(app.bot, group_text(122, "моя анкета", telegram_id=MEMBER_ID))
    assert "Профиль" in await last_reply(app)


async def test_chat_links_and_topic_title(app):
    await app.dispatcher.feed_update(app.bot, group_text(140, "+чат ссылка", telegram_id=MODERATOR_ID))
    assert "https://t.me/+mellow-test" in await last_reply(app)
    assert "CreateChatInviteLink" in app.session.call_names

    await app.dispatcher.feed_update(app.bot, group_text(141, "чат-ссылка", telegram_id=MEMBER_ID))
    assert "https://t.me/+mellow-test" in await last_reply(app)

    await app.dispatcher.feed_update(app.bot, group_text(142, "+чат ссылка по заявкам",
                                                         telegram_id=MODERATOR_ID))
    join_request = next(call for call in app.session.calls
                        if type(call).__name__ == "CreateChatInviteLink" and call.creates_join_request)
    assert join_request is not None

    await app.dispatcher.feed_update(app.bot, group_text(143, "сброс ссылок", telegram_id=MODERATOR_ID))
    assert "Отозвано ссылок: 2" in await last_reply(app)

    await app.dispatcher.feed_update(app.bot, group_text(144, "топик название Общее",
                                                         telegram_id=MODERATOR_ID))
    assert "работает в топике" in await last_reply(app)

    await app.dispatcher.feed_update(app.bot, group_text(145, "тг права @mod", telegram_id=MODERATOR_ID))
    assert "Статус" in await last_reply(app)


async def test_welcome_variables_are_substituted(app):
    await app.dispatcher.feed_update(app.bot, group_text(146, "+приветствие Привет, {имя}!",
                                                         telegram_id=MODERATOR_ID))
    app.session.calls.clear()
    await app.dispatcher.feed_update(app.bot, join_update(147, 761, "Ваня"))
    assert any("Привет, Ваня!" in text for text in app.session.sent_texts)

    await app.dispatcher.feed_update(app.bot, group_text(148, "приветствие", telegram_id=MEMBER_ID))
    assert "Привет, {имя}!" in await last_reply(app)


async def test_deleted_accounts_are_listed_and_filtered(app):
    async with app.session_factory() as session, session.begin():
        session.add(ChatMemberActivity(chat_id=GROUP_ID, telegram_id=AdminSession.DELETED_ACCOUNT_ID,
                                       joined_at=utcnow(), last_message_at=utcnow(), is_member=True))
    await app.dispatcher.feed_update(app.bot, group_text(150, "кто удалён", telegram_id=MODERATOR_ID))
    assert str(AdminSession.DELETED_ACCOUNT_ID) in await last_reply(app)


async def test_channel_messages_are_blocked_when_denied(app):
    from aiogram.types import Chat as TgChat

    await app.dispatcher.feed_update(app.bot, group_text(151, "-каналы", telegram_id=MODERATOR_ID))
    update = Update(update_id=152, message=Message(
        message_id=152, date=datetime.now(timezone.utc),
        chat=Chat(id=GROUP_ID, type="supergroup", title="Mellow"),
        sender_chat=TgChat(id=-1009, type="channel", title="Spam"),
        from_user=TelegramUser(id=0, is_bot=False, first_name="Spam"),
        text="реклама"))
    app.session.calls.clear()
    await app.dispatcher.feed_update(app.bot, update)
    assert 152 in app.session.deleted_messages
    assert "BanChatSenderChat" in app.session.call_names


async def test_minreg_and_autokick(app):
    await app.dispatcher.feed_update(app.bot, group_text(153, "+минрег 1", telegram_id=MODERATOR_ID))
    assert (await config_of(app)).minreg_days == 1

    async with app.session_factory() as session, session.begin():
        session.add(User(telegram_id=1001, username="fresh", created_at=utcnow()))
    app.session.calls.clear()
    await app.dispatcher.feed_update(app.bot, join_update(154, 1001, "Fresh"))
    assert "BanChatMember" in app.session.call_names  # свежий аккаунт исключён

    await app.dispatcher.feed_update(app.bot, group_text(155, "-минрег", telegram_id=MODERATOR_ID))
    assert (await config_of(app)).minreg_days is None

    await app.dispatcher.feed_update(app.bot, group_text(156, "+автокик 3 60 бан", telegram_id=MODERATOR_ID))
    config = await config_of(app)
    assert config.autokick_count == 3 and config.autokick_action == "бан"
    await app.dispatcher.feed_update(app.bot, group_text(157, "+входы", telegram_id=MODERATOR_ID))
    assert (await config_of(app)).notify_joins is True


async def test_grid_telegram_admins_and_topic_lock(app):
    await app.dispatcher.feed_update(app.bot, group_text(158, "дк установить сетку Тест",
                                                         telegram_id=OWNER_ID))
    app.session.calls.clear()
    await app.dispatcher.feed_update(app.bot, group_text(159, "сетка тг админ Модератор @player",
                                                         telegram_id=OWNER_ID))
    reply = await last_reply(app)
    assert "назначен" in reply and "1 чатах" in reply
    names = app.session.call_names
    assert "PromoteChatMember" in names and "SetChatAdministratorCustomTitle" in names

    await app.dispatcher.feed_update(app.bot, group_text(160, "сетка тг права @player",
                                                         telegram_id=OWNER_ID))
    assert "Статус" in await last_reply(app)

    await app.dispatcher.feed_update(app.bot, group_text(161, "-топик", telegram_id=MODERATOR_ID))
    assert "работает в топике" in await last_reply(app)


async def test_topic_is_closed_inside_a_topic(app):
    update = Update(update_id=162, message=Message(
        message_id=162, date=datetime.now(timezone.utc), message_thread_id=7,
        chat=Chat(id=GROUP_ID, type="supergroup", title="Mellow", is_forum=True),
        from_user=TelegramUser(id=MODERATOR_ID, is_bot=False, first_name="Moderator"),
        text="+топик"))
    app.session.calls.clear()
    await app.dispatcher.feed_update(app.bot, update)
    assert "CloseForumTopic" in app.session.call_names
    assert "закрыт" in await last_reply(app)

    update = Update(update_id=163, message=Message(
        message_id=163, date=datetime.now(timezone.utc), message_thread_id=7,
        chat=Chat(id=GROUP_ID, type="supergroup", title="Mellow", is_forum=True),
        from_user=TelegramUser(id=MODERATOR_ID, is_bot=False, first_name="Moderator"),
        text="-топик"))
    app.session.calls.clear()
    await app.dispatcher.feed_update(app.bot, update)
    assert "ReopenForumTopic" in app.session.call_names


async def test_autokick_bans_after_repeated_exits(app):
    await app.dispatcher.feed_update(app.bot, group_text(164, "+автокик 2 60 бан",
                                                         telegram_id=MODERATOR_ID))
    for update_id in (165, 166):
        member = TelegramUser(id=MEMBER_ID, is_bot=False, first_name="Player")
        await app.dispatcher.feed_update(app.bot, Update(update_id=update_id, message=Message(
            message_id=update_id, date=datetime.now(timezone.utc),
            chat=Chat(id=GROUP_ID, type="supergroup", title="Mellow"),
            from_user=member, left_chat_member=member)))
    bans = [row for row in await punishments(app, "ban") if row.active]
    assert len(bans) == 1 and bans[0].reason == "Автокик: частые выходы"
