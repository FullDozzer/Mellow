"""Startup checks that turn silent misconfiguration into an explicit warning.

The most common reason a working bot "loses" applications is an environment value:
a wrong chat ID, a bot that is not in the chat, or a forum chat where the bot has no
"manage topics" right. None of that is visible from the outside, so the bot now
checks its own configuration on start and reports problems to the admin chat.
"""

from __future__ import annotations

import html
import logging

from aiogram import Bot

from mellow.config import Settings
from mellow.delivery import safe_error

log = logging.getLogger("mellow.preflight")

FORUM_CHATS = (("APPLICATIONS_CHAT_ID", "applications_chat_id"),
               ("SUPPORT_CHAT_ID", "support_chat_id"),
               ("SUGGESTIONS_CHAT_ID", "suggestions_chat_id"),
               ("ADMIN_CHAT_ID", "administration_chat_id"))


async def configuration_problems(bot: Bot, settings: Settings) -> list[str]:
    problems: list[str] = []
    for name, attribute in FORUM_CHATS:
        chat_id = getattr(settings, attribute)
        if not chat_id:
            continue
        try:
            chat = await bot.get_chat(chat_id)
        except Exception as exc:
            problems.append(f"{name}={chat_id}: чат недоступен ({safe_error(exc)})")
            continue
        is_forum = bool(getattr(chat, "is_forum", False))
        if not is_forum:
            problems.append(f"{name}={chat_id}: темы (Topics) выключены — заявки и обращения "
                            "будут приходить обычными сообщениями в общий чат")
            continue
        try:
            member = await bot.get_chat_member(chat_id, (await bot.me()).id)
        except Exception as exc:
            problems.append(f"{name}={chat_id}: не удалось проверить права бота ({safe_error(exc)})")
            continue
        if not getattr(member, "can_manage_topics", False):
            problems.append(f"{name}={chat_id}: боту не выдано право «Управлять темами» — "
                            "заявки будут приходить обычными сообщениями")
    return problems


async def report_configuration(bot: Bot, settings: Settings) -> list[str]:
    """Log every problem and tell the staff chat about them; never fail startup."""
    try:
        problems = await configuration_problems(bot, settings)
    except Exception as exc:
        log.warning("Configuration check could not run: %s", safe_error(exc))
        return []
    if not problems:
        log.info("Chat configuration looks usable")
        return []
    for problem in problems:
        log.error("Configuration problem: %s", problem)
    try:
        body = ("⚠️ <b>Проблемы настройки Mellow</b>\n\n"
                + "\n".join(f"• {html.escape(problem)}" for problem in problems)
                + "\n\nПока проблемы не устранены, бот доставляет заявки в общий чат и повторяет попытки.")
        await bot.send_message(settings.administration_chat_id, body, parse_mode="HTML")
    except Exception as exc:
        log.warning("Configuration problems could not be reported to ADMIN_CHAT_ID: %s", safe_error(exc))
    return problems
