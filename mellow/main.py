from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from mellow.chatadmin.commands import router as chatadmin_router
from mellow.chatadmin.config import ChatSettingsStore
from mellow.chatadmin.guard import ChatGuard, RecentMessages
from mellow.config import load_settings
from mellow.db import create_database, create_schema
from mellow.handlers import router
from mellow.middleware import PrivacySafeMessageCounter, outbox_worker
from mellow.minecraft import MinecraftClient
from mellow.preflight import report_configuration
from mellow.services import apply_staff_configuration


async def main():
    settings = load_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    engine, session_factory = create_database(settings.database_url)
    await create_schema(engine)
    await apply_staff_configuration(session_factory, settings)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    # A wrong chat ID or a missing "manage topics" right is the usual reason a working
    # bot seems to lose applications; say it out loud instead of failing silently.
    await report_configuration(bot, settings)
    dispatcher = Dispatcher()
    dispatcher["settings"] = settings
    dispatcher["session_factory"] = session_factory
    dispatcher["minecraft"] = MinecraftClient(settings.minecraft_api_url, settings.minecraft_api_token, settings.minecraft_api_timeout)
    store = ChatSettingsStore(session_factory)
    recent = RecentMessages()
    dispatcher["store"] = store
    dispatcher["recent"] = recent
    dispatcher.update.outer_middleware(PrivacySafeMessageCounter(settings, session_factory))
    # The guard runs after the counter: it deletes filtered messages and applies the
    # punishments the chat configured, without storing any message text.
    dispatcher.update.outer_middleware(ChatGuard(settings, session_factory, store, recent))
    dispatcher.include_router(router)
    dispatcher.include_router(chatadmin_router)
    outbox_task = asyncio.create_task(outbox_worker(bot, settings, session_factory), name="mellow-outbox")
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        outbox_task.cancel()
        with suppress(asyncio.CancelledError):
            await outbox_task
        await bot.session.close()
        await engine.dispose()


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
