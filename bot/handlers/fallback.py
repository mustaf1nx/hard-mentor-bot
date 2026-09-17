"""Последний роутер: всё, что не поймали остальные."""
from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery, Message

from .. import views
from ..repo import Viewer
from ..texts import t

router = Router(name="fallback")


@router.callback_query()
async def stale_button(callback: CallbackQuery, viewer: Viewer) -> None:
    # Типичный случай — бот перезапустился (редеплой) и забыл незавершённую анкету.
    await callback.answer(t(viewer.lang, "expired"), show_alert=True)


@router.message()
async def anything_else(message: Message, viewer: Viewer) -> None:
    await message.answer(t(viewer.lang, "fallback"), reply_markup=views.main_menu(viewer.lang, viewer.is_staff))
