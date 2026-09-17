"""Общие команды: /start, /help, /lang, /myid, /cancel."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import async_sessionmaker

from .. import repo, views
from ..config import Settings
from ..repo import Viewer
from ..texts import menu_labels, t

router = Router(name="common")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, viewer: Viewer) -> None:
    await state.clear()
    await message.answer(t(viewer.lang, "start"), reply_markup=views.main_menu(viewer.lang, viewer.is_staff))


@router.message(Command("help"))
@router.message(F.text.in_(menu_labels("help")))
async def cmd_help(message: Message, viewer: Viewer, settings: Settings) -> None:
    text = t(viewer.lang, "help", limit=settings.daily_limit)
    if viewer.is_staff:
        text += t(viewer.lang, "help_staff")
    await message.answer(text, reply_markup=views.main_menu(viewer.lang, viewer.is_staff))


@router.message(Command("lang"))
async def cmd_lang(message: Message, viewer: Viewer, sm: async_sessionmaker) -> None:
    new_lang = "en" if viewer.lang == "ru" else "ru"
    async with sm() as session:
        await repo.set_lang(session, viewer.id, new_lang)
    await message.answer(t(new_lang, "lang_set"), reply_markup=views.main_menu(new_lang, viewer.is_staff))


@router.message(Command("myid"))
async def cmd_myid(message: Message, viewer: Viewer) -> None:
    await message.answer(t(viewer.lang, "myid", id=viewer.id))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, viewer: Viewer) -> None:
    if await state.get_state() is None:
        await message.answer(t(viewer.lang, "nothing_to_cancel"))
        return
    await state.clear()
    await message.answer(t(viewer.lang, "cancelled"), reply_markup=views.main_menu(viewer.lang, viewer.is_staff))
