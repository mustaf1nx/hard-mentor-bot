"""Мидлварь: определяет, кто пишет, его роль и язык. Работает только в личке."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import async_sessionmaker

from . import repo
from .config import Settings
from .texts import detect_lang


class ViewerMiddleware(BaseMiddleware):
    def __init__(self, sm: async_sessionmaker, settings: Settings) -> None:
        self.sm = sm
        self.settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        chat = data.get("event_chat")
        if tg_user is None or tg_user.is_bot:
            return None
        if chat is not None and chat.type != "private":
            return None  # в группах молчим: обращения — дело личное
        async with self.sm() as session:
            user = await repo.upsert_user(
                session, tg_user.id, tg_user.full_name, tg_user.username,
                detect_lang(tg_user.language_code),
            )
        data["viewer"] = repo.Viewer(
            id=user.telegram_id,
            role=self.settings.role_for(user.telegram_id, user.role),
            lang=user.lang,
            name=user.display_name,
            username=user.username,
        )
        return await handler(event, data)
