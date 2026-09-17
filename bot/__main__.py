"""Точка входа: python -m bot"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat

from . import reports
from .app import build_dispatcher
from .config import Settings
from .db import init_db, make_engine, make_sessionmaker

log = logging.getLogger("bot")

USER_COMMANDS = {
    "ru": [("feedback", "Оставить обращение"), ("status", "Мои обращения"), ("help", "Помощь"),
           ("cancel", "Отменить заполнение"), ("lang", "Сменить язык")],
    "en": [("feedback", "Send feedback"), ("status", "My tickets"), ("help", "Help"),
           ("cancel", "Cancel submission"), ("lang", "Switch language")],
}
STAFF_COMMANDS = [("queue", "Очередь / Queue"), ("report", "Сводка / Summary"), ("mentors", "Менторы / Mentors")]


async def setup_commands(bot: Bot, settings: Settings) -> None:
    ru = [BotCommand(command=c, description=d) for c, d in USER_COMMANDS["ru"]]
    en = [BotCommand(command=c, description=d) for c, d in USER_COMMANDS["en"]]
    await bot.set_my_commands(ru)
    await bot.set_my_commands(en, language_code="en")
    staff = [BotCommand(command=c, description=d) for c, d in STAFF_COMMANDS]
    for uid in settings.staff_ids:
        try:
            await bot.set_my_commands(ru + staff, scope=BotCommandScopeChat(chat_id=uid))
        except TelegramAPIError:
            log.warning("Сотрудник %s ещё не нажал /start — меню команд для него не выставлено", uid)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # aiogram на INFO пишет по строке на каждый апдейт. ID пользователя там нет, но лишние
    # метки времени в логах Railway нам ни к чему — см. раздел 10 спека про ре-идентификацию.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)  # SQL с параметрами в логи не пишем

    settings = Settings.from_env()
    if not settings.coordinator_ids:
        log.warning("COORDINATOR_IDS пуст — обращения будут уходить только руководителю")
    if not settings.lead_ids:
        log.warning("LEAD_IDS пуст — жалобы на координатора и эскалации отключены")

    engine = make_engine(settings.database_url, settings.db_connect_args)
    await init_db(engine)
    sm = make_sessionmaker(engine)

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher(settings, sm)
    background: list[asyncio.Task] = []
    try:
        await setup_commands(bot, settings)
        background = [
            asyncio.create_task(reports.weekly_report_loop(bot, sm, settings)),
            asyncio.create_task(reports.sla_watch_loop(bot, sm, settings)),
        ]
        # Long polling: порт и домен не нужны. drop_pending_updates не ставим —
        # сообщения, пришедшие во время редеплоя, не должны теряться.
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await bot.session.close()
        await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
