"""Сторона заявителя после отправки: /status, дополнения, оценка (раздел 6.4)."""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import async_sessionmaker

from .. import notify, repo, views
from ..config import Settings
from ..db import Ticket
from ..ratelimit import DailyCounter
from ..repo import Viewer
from ..texts import menu_labels, t

router = Router(name="status")

_CODE_RE = re.compile(r"^\s*(?:HM-?)?0*(\d{1,9})\s+([A-Za-z0-9-]{8,12})\s*$", re.IGNORECASE)


class Comment(StatesGroup):
    text = State()


def _owns(viewer: Viewer, ticket: Ticket, token: str, settings: Settings) -> bool:
    """Именной тикет — по submitter_id. Анонимный — по подписи, которую бот выдаёт
    только после предъявления кода доступа."""
    if ticket.is_anonymous:
        return bool(token) and token == repo.ticket_token(settings.secret, ticket.id)
    return ticket.submitter_id == viewer.id


async def _send_card(message: Message, sm: async_sessionmaker, ticket: Ticket, lang: str, token: str = "") -> None:
    async with sm() as session:
        notes = await repo.list_notes(session, ticket.id, visible_only=True)
    parts = views.submitter_card(ticket, notes, lang)
    for part in parts[:-1]:
        await message.answer(part)
    await message.answer(parts[-1], reply_markup=views.submitter_card_kb(ticket, lang, token))
    if ticket.status == "resolved" and not ticket.satisfaction_rating and ticket.category != "praise":
        await message.answer(t(lang, "ask_rating", code=ticket.code), reply_markup=views.rating_kb(ticket.id, token))


@router.message(Command("status"))
async def cmd_status(message: Message, command: CommandObject, viewer: Viewer, settings: Settings,
                     sm: async_sessionmaker, code_limiter: DailyCounter) -> None:
    lang = viewer.lang
    if not command.args:
        await list_my_tickets(message, viewer, sm)
        return
    match = _CODE_RE.match(command.args)
    if not match:
        await message.answer(t(lang, "status_usage"))
        return
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    if not code_limiter.allowed(viewer.id, today):
        await message.answer(t(lang, "too_many_attempts"))
        return
    async with sm() as session:
        ticket = await repo.get_ticket(session, int(match.group(1)))
    if ticket is None or not repo.check_access_code(settings.secret, ticket, match.group(2)):
        code_limiter.hit(viewer.id, today)
        await message.answer(t(lang, "ticket_not_found"))
        return
    await _send_card(message, sm, ticket, lang, repo.ticket_token(settings.secret, ticket.id))


@router.message(F.text.in_(menu_labels("status")))
async def list_my_tickets(message: Message, viewer: Viewer, sm: async_sessionmaker) -> None:
    async with sm() as session:
        tickets = await repo.list_user_tickets(session, viewer.id)
    if not tickets:
        await message.answer(t(viewer.lang, "no_tickets"))
        return
    await message.answer(t(viewer.lang, "my_tickets"), reply_markup=views.my_tickets_kb(tickets, viewer.lang))


@router.callback_query(F.data.startswith("my:"))
async def open_my_ticket(callback: CallbackQuery, viewer: Viewer, settings: Settings, sm: async_sessionmaker) -> None:
    ticket_id = callback.data.split(":")[1]
    async with sm() as session:
        ticket = await repo.get_ticket(session, int(ticket_id)) if ticket_id.isdigit() else None
    if ticket is None or not _owns(viewer, ticket, "", settings):
        await callback.answer(t(viewer.lang, "ticket_not_found"), show_alert=True)
        return
    await callback.answer()
    await _send_card(callback.message, sm, ticket, viewer.lang)


# ---------- дополнение от заявителя ----------

@router.callback_query(F.data.startswith("cm:"))
async def ask_comment(callback: CallbackQuery, state: FSMContext, viewer: Viewer,
                      settings: Settings, sm: async_sessionmaker) -> None:
    parts = callback.data.split(":")
    token = parts[2] if len(parts) > 2 else ""
    async with sm() as session:
        ticket = await repo.get_ticket(session, int(parts[1])) if parts[1].isdigit() else None
    if ticket is None or not _owns(viewer, ticket, token, settings) or ticket.status == "closed":
        await callback.answer(t(viewer.lang, "ticket_not_found"), show_alert=True)
        return
    await callback.answer()
    await state.set_state(Comment.text)
    await state.update_data(ticket_id=ticket.id)
    await callback.message.answer(t(viewer.lang, "ask_comment", code=ticket.code))


@router.message(Comment.text, F.text & ~F.text.startswith("/"))
async def got_comment(message: Message, state: FSMContext, viewer: Viewer, settings: Settings,
                      sm: async_sessionmaker, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    text = views.clip(message.text.strip(), 3000)
    async with sm() as session:
        ticket = await repo.get_ticket(session, data.get("ticket_id", 0))
        if ticket is None:
            await message.answer(t(viewer.lang, "ticket_not_found"))
            return
        await repo.add_note(session, ticket, text=text, visible=True, author_kind="submitter")
    await message.answer(t(viewer.lang, "comment_saved", code=ticket.code))
    await notify.notify_submitter_comment(bot, sm, settings, ticket, text)


# ---------- оценка 1–5 ----------

@router.callback_query(F.data.startswith("rate:"))
async def rate(callback: CallbackQuery, viewer: Viewer, settings: Settings, sm: async_sessionmaker) -> None:
    parts = callback.data.split(":")
    token = parts[3] if len(parts) > 3 else ""
    valid = len(parts) >= 3 and parts[1].isdigit() and parts[2] in {"1", "2", "3", "4", "5"}
    async with sm() as session:
        ticket = await repo.get_ticket(session, int(parts[1])) if valid else None
        if ticket is None or not _owns(viewer, ticket, token, settings):
            await callback.answer(t(viewer.lang, "ticket_not_found"), show_alert=True)
            return
        if ticket.satisfaction_rating:
            await callback.answer(t(viewer.lang, "rating_done"))
            return
        await repo.set_rating(session, ticket, int(parts[2]))
    await callback.answer(t(viewer.lang, "rating_thanks"))
    try:
        await callback.message.edit_text(f"{t(viewer.lang, 'rating_thanks')} {'⭐' * int(parts[2])}")
    except Exception:
        pass
