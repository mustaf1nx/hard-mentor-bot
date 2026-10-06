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
    await state.set_data({"category": category, "lead_only": False})
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
            await callback.message.answer(t(lang, "ask_text"), reply_markup=views.feedback_nav_kb(lang))
    elif category in ASKS_RELATED:
        async with sm() as session:
            mentors = await repo.list_mentors(session)
        await state.set_state(Feedback.related)
        await callback.message.answer(
            t(lang, "ask_related"), reply_markup=views.related_kb(lang, category, mentors, has_lead)
        )
    else:
        await state.set_state(Feedback.text)
        await callback.message.answer(t(lang, "ask_text"), reply_markup=views.feedback_nav_kb(lang))


# ---------- навигация анкеты ----------

FEEDBACK_STATE_NAMES = {
    Feedback.category.state, Feedback.related.state, Feedback.coord.state,
    Feedback.text.state, Feedback.attachment.state, Feedback.anon.state,
}


async def _feedback_cancel(callback: CallbackQuery, state: FSMContext, viewer: Viewer) -> None:
    if await state.get_state() not in FEEDBACK_STATE_NAMES:
        await callback.answer(t(viewer.lang, "expired"), show_alert=True)
        return
    await callback.answer()
    await _strip_buttons(callback)
    await state.clear()
    await callback.message.answer(
        t(viewer.lang, "cancelled"),
        reply_markup=views.main_menu(viewer.lang, viewer.is_staff),
    )


@router.callback_query(F.data == "fb:cancel")
async def cancel_feedback(callback: CallbackQuery, state: FSMContext, viewer: Viewer) -> None:
    await _feedback_cancel(callback, state, viewer)


@router.callback_query(F.data == "fb:back")
async def back_feedback(callback: CallbackQuery, state: FSMContext, viewer: Viewer,
                        settings: Settings, sm: async_sessionmaker) -> None:
    current = await state.get_state()
    if current not in FEEDBACK_STATE_NAMES:
        await callback.answer(t(viewer.lang, "expired"), show_alert=True)
        return

    data = await state.get_data()
    category = data.get("category")
    await callback.answer()
    await _strip_buttons(callback)

    if current in (Feedback.related.state, Feedback.coord.state):
        await state.clear()
        await state.set_state(Feedback.category)
        await callback.message.answer(
            t(viewer.lang, "choose_category"),
            reply_markup=views.categories_kb(viewer.lang),
        )
        return

    if current == Feedback.text.state:
        data.pop("text", None)
        data.pop("file_id", None)
        data.pop("file_type", None)
        if category in ASKS_RELATED:
            data.pop("mentor_id", None)
            data.pop("related_course", None)
            data["lead_only"] = False
            await state.set_data(data)
            async with sm() as session:
                mentors = await repo.list_mentors(session)
            await state.set_state(Feedback.related)
            await callback.message.answer(
                t(viewer.lang, "ask_related"),
                reply_markup=views.related_kb(
                    viewer.lang, category, mentors, bool(settings.lead_ids),
                ),
            )
            return
        if category == "serious" and settings.lead_ids:
            data["lead_only"] = False
            await state.set_data(data)
            await state.set_state(Feedback.coord)
            await callback.message.answer(
                t(viewer.lang, "serious_intro"),
                reply_markup=views.serious_kb(viewer.lang),
            )
            return
        await state.clear()
        await state.set_state(Feedback.category)
        await callback.message.answer(
            t(viewer.lang, "choose_category"),
            reply_markup=views.categories_kb(viewer.lang),
        )
        return

    if current == Feedback.attachment.state:
        data.pop("text", None)
        data.pop("file_id", None)
        data.pop("file_type", None)
        await state.set_data(data)
        await state.set_state(Feedback.text)
        await callback.message.answer(
            t(viewer.lang, "ask_text"),
            reply_markup=views.feedback_nav_kb(viewer.lang),
        )
        return

    if current == Feedback.anon.state:
        data.pop("file_id", None)
        data.pop("file_type", None)
        await state.set_data(data)
        await state.set_state(Feedback.attachment)
        await callback.message.answer(
            t(viewer.lang, "ask_attachment"),
            reply_markup=views.skip_kb(viewer.lang, "att:skip"),
        )
        return

    # На первом шаге отдельной кнопки «Назад» нет; если старое сообщение всё же нажали,
    # безопасно трактуем это как выход из анкеты.
    await state.clear()
    await callback.message.answer(
        t(viewer.lang, "cancelled"),
        reply_markup=views.main_menu(viewer.lang, viewer.is_staff),
    )


# ---------- шаг 2: к чему относится / касается ли координатора ----------

@router.callback_query(Feedback.related, F.data.startswith("rel:"))
@router.callback_query(Feedback.coord, F.data.startswith("rel:"))
async def pick_related(callback: CallbackQuery, state: FSMContext, viewer: Viewer,
                       settings: Settings, sm: async_sessionmaker) -> None:
    parts = callback.data.split(":")
    data = await state.get_data()
    if len(parts) == 3 and parts[1] == "m" and parts[2].isdigit() and len(parts[2]) <= 19:
        mentor_id = int(parts[2])
        if mentor_id > (1 << 63) - 1 or data.get("category") not in ASKS_RELATED:
            await callback.answer(t(viewer.lang, "no_access"), show_alert=True)
            return
        async with sm() as session:
            known = await repo.is_mentor(session, mentor_id)
        if not known:
            await callback.answer(t(viewer.lang, "mentor_unavailable"), show_alert=True)
            return
        data["mentor_id"] = mentor_id
        data.pop("related_course", None)
        data["lead_only"] = False
        await state.set_data(data)
    elif parts == ["rel", "coord"] and settings.lead_ids and data.get("category") in ("complaint", "serious"):
        data.pop("mentor_id", None)
        data.pop("related_course", None)
        data["lead_only"] = True
        await state.set_data(data)
        await callback.message.answer(t(viewer.lang, "lead_only_note"))
    elif parts == ["rel", "skip"]:
        data.pop("mentor_id", None)
        data.pop("related_course", None)
        data["lead_only"] = False
        await state.set_data(data)
    else:
        await callback.answer(t(viewer.lang, "no_access"), show_alert=True)
        return
    await callback.answer()
    await _strip_buttons(callback)
    await state.set_state(Feedback.text)
    await callback.message.answer(t(viewer.lang, "ask_text"), reply_markup=views.feedback_nav_kb(viewer.lang))


@router.callback_query(Feedback.related, F.data.startswith("relpage:"))
async def related_page(callback: CallbackQuery, state: FSMContext, viewer: Viewer,
                       settings: Settings, sm: async_sessionmaker) -> None:
    raw = callback.data.split(":", 1)[1]
    data = await state.get_data()
    if not raw.isdigit() or len(raw) > 9 or data.get("category") not in ASKS_RELATED:
        await callback.answer(t(viewer.lang, "expired"), show_alert=True)
        return
    async with sm() as session:
        mentors = await repo.list_mentors(session)
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=views.related_kb(
            viewer.lang, data["category"], mentors, bool(settings.lead_ids), page=int(raw),
        ))
    except (TelegramBadRequest, AttributeError):
        pass


@router.message(Feedback.related, F.text & ~F.text.startswith("/"))
async def typed_related(message: Message, state: FSMContext, viewer: Viewer) -> None:
    data = await state.get_data()
    data["related_course"] = message.text.strip()[:200]
    data.pop("mentor_id", None)
    data["lead_only"] = False
    await state.set_data(data)
    await state.set_state(Feedback.text)
    await message.answer(t(viewer.lang, "ask_text"), reply_markup=views.feedback_nav_kb(viewer.lang))


# ---------- шаг 3: текст ----------

@router.message(Feedback.text)
@router.message(Feedback.coord)  # на «серьёзном» человек может сразу начать писать — не мешаем
async def got_text(message: Message, state: FSMContext, viewer: Viewer) -> None:
    lang = viewer.lang
    text = (message.text or "").strip()
    if not text:
        await message.answer(t(lang, "text_needed"), reply_markup=views.feedback_nav_kb(lang))
        return
    if text.startswith("/"):
        await message.answer(t(lang, "text_is_command"), reply_markup=views.feedback_nav_kb(lang))
        return
    if len(text) < MIN_TEXT:
        await message.answer(t(lang, "text_too_short", min=MIN_TEXT), reply_markup=views.feedback_nav_kb(lang))
        return
    if views.u16len(text) > MAX_TEXT:
        await message.answer(t(lang, "text_too_long", n=views.u16len(text), max=MAX_TEXT), reply_markup=views.feedback_nav_kb(lang))
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
    data = await state.get_data()
    data.pop("file_id", None)
    data.pop("file_type", None)
    await state.set_data(data)
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
    if mentor_id:
        async with sm() as session:
            active = await repo.is_mentor(session, mentor_id)
            former = await repo.get_user(session, mentor_id) if not active else None
        if not active:
            # Removed while the submitter was filling in the form: route to admins
            # instead of sending sensitive content to a former mentor.
            data["related_course"] = data.get("related_course") or (former.display_name if former else str(mentor_id))
            mentor_id = None
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
            notification_settings=settings,
        )
    limiter.hit(viewer.id, today)
    # В лог — только номер и категория. Никаких ID заявителя, даже для именных обращений.
    log.info("Создано обращение %s (%s)", ticket.code, category)

    sla = t(lang, "sla_lead_only") if lead_only else t(lang, f"sla_{category}")
    if is_anonymous:
        text = t(lang, "submitted_anon", code=ticket.code, sla=sla, secret=secret_code)
    else:
        text = t(lang, "submitted", code=ticket.code, sla=sla)
    try:
        await callback.message.answer(text, reply_markup=views.main_menu(lang, viewer.is_staff))
    finally:
        # Still notify staff if the submitter blocks the bot immediately after
        # submission. The persistent worker also recovers after a process exit.
        await notify.notify_new_ticket(bot, sm, settings, ticket)
