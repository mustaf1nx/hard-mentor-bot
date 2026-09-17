"""Сборка диспетчера. Вынесена отдельно, чтобы тесты собирали бота так же, как прод."""
from __future__ import annotations

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import async_sessionmaker

from .config import Settings
from .handlers import common, fallback, feedback, staff, status
from .middleware import ViewerMiddleware
from .ratelimit import DailyCounter


def build_dispatcher(settings: Settings, sm: async_sessionmaker) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp["settings"] = settings
    dp["sm"] = sm
    dp["limiter"] = DailyCounter(settings.daily_limit)
    dp["code_limiter"] = DailyCounter(10)  # неверные попытки ввода кода анонимного тикета
    dp.update.outer_middleware(ViewerMiddleware(sm, settings))
    # Порядок важен: команды и кнопки меню должны срабатывать раньше, чем шаги анкеты
    # («📋 Мои обращения» не должно стать текстом жалобы).
    for module in (common, status, staff, feedback, fallback):
        dp.include_router(module.router)
    return dp
