from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from mellow.config import load_settings
from mellow.db import create_database, create_schema
from mellow.handlers import router
from mellow.middleware import PrivacySafeMessageCounter, outbox_worker
from mellow.minecraft import MinecraftClient
from mellow.services import apply_staff_configuration


async def main():
    settings = load_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    engine, session_factory = create_database(settings.database_url)
    await create_schema(session_factory)
    await apply_staff_configuration(session_factory, settings)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher["settings"] = settings
    dispatcher["session_factory"] = session_factory
    dispatcher["minecraft"] = MinecraftClient(settings.minecraft_api_url, settings.minecraft_api_token, settings.minecraft_api_timeout)
    dispatcher.update.outer_middleware(PrivacySafeMessageCounter(settings, session_factory))
    dispatcher.include_router(router)
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
