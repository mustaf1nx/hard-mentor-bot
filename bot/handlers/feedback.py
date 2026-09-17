"""Сценарий подачи обращения (раздел 6.1 спека).

Состояние диалога живёт в памяти процесса: незавершённая анкета нигде не сохраняется.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import async_sessionmaker

from .. import moderation, notify, repo, views
from ..config import Settings
from ..db import CATEGORIES
from ..ratelimit import DailyCounter
from ..repo import Viewer
from ..texts import menu_labels, t

log = logging.getLogger(__name__)
router = Router(name="feedback")

MIN_TEXT = 10
MAX_TEXT = 3000  # в UTF-16 юнитах: карточка с шапкой должна влезать в 4096
ASKS_RELATED = ("question", "complaint", "praise")


class Feedback(StatesGroup):
    category = State()
    related = State()
    coord = State()
    text = State()
    attachment = State()
    anon = State()


def _today(settings: Settings):
    return datetime.now(ZoneInfo(settings.timezone)).date()


async def _strip_buttons(callback: CallbackQuery) -> None:
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except (TelegramBadRequest, AttributeError):
        pass


# ---------- шаг 1: категория ----------

@router.message(Command("feedback"))
@router.message(F.text.in_(menu_labels("feedback")))
async def start_feedback(message: Message, state: FSMContext, viewer: Viewer,
                         settings: Settings, limiter: DailyCounter) -> None:
    await state.clear()
    if not limiter.allowed(viewer.id, _today(settings)):
        await message.answer(t(viewer.lang, "rate_limited", limit=settings.daily_limit))
        return
    await state.set_state(Feedback.category)
    await message.answer(t(viewer.lang, "choose_category"), reply_markup=views.categories_kb(viewer.lang))


@router.callback_query(Feedback.category, F.data.startswith("cat:"))
async def pick_category(callback: CallbackQuery, state: FSMContext, viewer: Viewer,
                        settings: Settings, sm: async_sessionmaker) -> None:
    category = callback.data.split(":", 1)[1]
    if category not in CATEGORIES:
        await callback.answer()
        return
    lang = viewer.lang
    await state.update_data(category=category, lead_only=False)
    await callback.answer()
    await _strip_buttons(callback)
    has_lead = bool(settings.lead_ids)

    if category == "serious":
        if has_lead:
            await state.set_state(Feedback.coord)
            await callback.message.answer(t(lang, "serious_intro"), reply_markup=views.serious_kb(lang))
        else:
            await state.set_state(Feedback.text)
            await callback.message.answer(t(lang, "serious_intro_nolead"))
            await callback.message.answer(t(lang, "ask_text"))
    elif category in ASKS_RELATED:
        async with sm() as session:
            mentors = await repo.list_mentors(session)
        await state.set_state(Feedback.related)
        await callback.message.answer(
            t(lang, "ask_related"), reply_markup=views.related_kb(lang, category, mentors, has_lead)
        )
    else:
        await state.set_state(Feedback.text)
        await callback.message.answer(t(lang, "ask_text"))


# ---------- шаг 2: к чему относится / касается ли координатора ----------

@router.callback_query(Feedback.related, F.data.startswith("rel:"))
@router.callback_query(Feedback.coord, F.data.startswith("rel:"))
async def pick_related(callback: CallbackQuery, state: FSMContext, viewer: Viewer, settings: Settings) -> None:
    parts = callback.data.split(":")
    await callback.answer()
    await _strip_buttons(callback)
    if parts[1] == "m" and len(parts) == 3 and parts[2].isdigit():
        await state.update_data(mentor_id=int(parts[2]))
    elif parts[1] == "coord" and settings.lead_ids:
        await state.update_data(lead_only=True)
        await callback.message.answer(t(viewer.lang, "lead_only_note"))
    await state.set_state(Feedback.text)
    await callback.message.answer(t(viewer.lang, "ask_text"))


@router.message(Feedback.related, F.text & ~F.text.startswith("/"))
async def typed_related(message: Message, state: FSMContext, viewer: Viewer) -> None:
    await state.update_data(related_course=message.text.strip()[:200])
    await state.set_state(Feedback.text)
    await message.answer(t(viewer.lang, "ask_text"))


# ---------- шаг 3: текст ----------

@router.message(Feedback.text)
@router.message(Feedback.coord)  # на «серьёзном» человек может сразу начать писать — не мешаем
async def got_text(message: Message, state: FSMContext, viewer: Viewer) -> None:
    lang = viewer.lang
    text = (message.text or "").strip()
    if not text:
        await message.answer(t(lang, "text_needed"))
        return
    if text.startswith("/"):
        await message.answer(t(lang, "text_is_command"))
        return
    if len(text) < MIN_TEXT:
        await message.answer(t(lang, "text_too_short", min=MIN_TEXT))
        return
    if views.u16len(text) > MAX_TEXT:
        await message.answer(t(lang, "text_too_long", n=views.u16len(text), max=MAX_TEXT))
        return
    await state.update_data(text=text)
    await state.set_state(Feedback.attachment)
    await message.answer(t(lang, "ask_attachment"), reply_markup=views.skip_kb(lang, "att:skip"))


# ---------- шаг 4: вложение ----------

async def _ask_anon(message: Message, state: FSMContext, lang: str) -> None:
    await state.set_state(Feedback.anon)
    await message.answer(t(lang, "ask_anon"), reply_markup=views.anon_kb(lang))


@router.callback_query(Feedback.attachment, F.data == "att:skip")
async def skip_attachment(callback: CallbackQuery, state: FSMContext, viewer: Viewer) -> None:
    await callback.answer()
    await _strip_buttons(callback)
    await _ask_anon(callback.message, state, viewer.lang)


@router.message(Feedback.attachment)
async def got_attachment(message: Message, state: FSMContext, viewer: Viewer) -> None:
    # Храним только file_id Telegram: файл не скачиваем и никуда не перекладываем.
    if message.photo:
        file_id, kind = message.photo[-1].file_id, "photo"
    elif message.document:
        file_id, kind = message.document.file_id, "document"
    elif message.video:
        file_id, kind = message.video.file_id, "video"
    else:
        await message.answer(t(viewer.lang, "attachment_needed"),
                             reply_markup=views.skip_kb(viewer.lang, "att:skip"))
        return
    await state.update_data(file_id=file_id, file_type=kind)
    await _ask_anon(message, state, viewer.lang)


# ---------- шаг 5: анонимность и отправка ----------

@router.callback_query(Feedback.anon, F.data.in_({"anon:0", "anon:1"}))
async def finish(callback: CallbackQuery, state: FSMContext, viewer: Viewer, settings: Settings,
                 sm: async_sessionmaker, limiter: DailyCounter, bot: Bot) -> None:
    lang = viewer.lang
    is_anonymous = callback.data == "anon:1"
    data = await state.get_data()
    await state.clear()
    await callback.answer()
    await _strip_buttons(callback)

    today = _today(settings)
    if not limiter.allowed(viewer.id, today):
        await callback.message.answer(t(lang, "rate_limited", limit=settings.daily_limit))
        return
    if not data.get("text") or data.get("category") not in CATEGORIES:
        await callback.message.answer(t(lang, "expired"))
        return

    category = data["category"]
    lead_only = bool(data.get("lead_only")) and bool(settings.lead_ids)
    mentor_id = data.get("mentor_id")
    flagged = ", ".join(moderation.check(data["text"])) or None
    secret_code = repo.new_access_code() if is_anonymous else None
    name = viewer.name + (f" (@{viewer.username})" if viewer.username else "")
    praise_delivered = category == "praise" and mentor_id is not None

    async with sm() as session:
        ticket = await repo.create_ticket(
            session,
            category=category,
            text=data["text"],
            is_anonymous=is_anonymous,
            submitter_id=viewer.id,
            submitter_name=name,
            related_course=data.get("related_course"),
            mentor_id=mentor_id,
            attachment_file_id=data.get("file_id"),
            attachment_type=data.get("file_type"),
            lead_only=lead_only,
            flagged=flagged,
            access_hash=repo.hash_access_code(settings.secret, secret_code) if secret_code else None,
            # Благодарность конкретному ментору доставляется сразу — очередь ей не нужна.
            status="closed" if praise_delivered else "new",
            assigned_to=mentor_id if praise_delivered else None,
        )
    limiter.hit(viewer.id, today)
    # В лог — только номер и категория. Никаких ID заявителя, даже для именных обращений.
    log.info("Создано обращение %s (%s)", ticket.code, category)

    sla = t(lang, "sla_lead_only") if lead_only else t(lang, f"sla_{category}")
    if is_anonymous:
        text = t(lang, "submitted_anon", code=ticket.code, sla=sla, secret=secret_code)
    else:
        text = t(lang, "submitted", code=ticket.code, sla=sla)
    await callback.message.answer(text, reply_markup=views.main_menu(lang, viewer.is_staff))

    await notify.notify_new_ticket(bot, sm, settings, ticket)
