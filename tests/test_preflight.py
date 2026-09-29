"""A wrong chat ID or a chat without topics must never stay invisible."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mellow.config import Level, Settings
from mellow.preflight import configuration_problems, report_configuration


def settings(applications_chat_id: int = -1001, administration_chat_id: int = -1002) -> Settings:
    return Settings(
        bot_token="test", database_url="sqlite+aiosqlite:///:memory:",
        applications_chat_id=applications_chat_id, support_chat_id=None, suggestions_chat_id=None,
        administration_chat_id=administration_chat_id, questions=[],
        levels={5: Level("Владелец", frozenset({"*"}), 0)},
    )


def bot_with(chat_ids: dict, *, is_forum: bool = True, can_manage_topics: bool = True) -> AsyncMock:
    bot = AsyncMock()
    bot.me.return_value = SimpleNamespace(id=1)

    async def get_chat(chat_id):
        if chat_id not in chat_ids:
            raise RuntimeError("chat not found")
        return SimpleNamespace(id=chat_id, is_forum=is_forum)

    bot.get_chat.side_effect = get_chat
    bot.get_chat_member.return_value = SimpleNamespace(can_manage_topics=can_manage_topics)
    return bot


async def test_unreachable_chat_is_reported():
    cfg = settings()
    problems = await configuration_problems(bot_with({}), cfg)
    assert any("APPLICATIONS_CHAT_ID" in problem and "недоступен" in problem for problem in problems)
    assert any("ADMIN_CHAT_ID" in problem for problem in problems)


async def test_chat_without_topics_is_reported_as_a_warning():
    cfg = settings()
    problems = await configuration_problems(bot_with({-1001: None, -1002: None}, is_forum=False), cfg)
    assert len(problems) == 2
    assert all("темы (Topics) выключены" in problem for problem in problems)


async def test_missing_manage_topics_right_is_reported():
    cfg = settings()
    problems = await configuration_problems(bot_with({-1001: None, -1002: None}, can_manage_topics=False), cfg)
    assert all("Управлять темами" in problem for problem in problems)


async def test_healthy_configuration_has_no_problems():
    cfg = settings()
    assert await configuration_problems(bot_with({-1001: None, -1002: None}), cfg) == []


async def test_reporting_never_fails_startup():
    cfg = settings()
    bot = bot_with({})
    bot.send_message.side_effect = RuntimeError("network is down")
    problems = await report_configuration(bot, cfg)
    assert problems
