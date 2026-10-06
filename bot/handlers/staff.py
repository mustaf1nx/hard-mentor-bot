"""Инструменты команды программы (раздел 6.6): очередь, карточка, статусы, ответы,
заметки, назначение, менторы, отчёт. Всё внутри Telegram — таблица не нужна."""
from __future__ import annotations

import re
from html import escape
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import async_sessionmaker

from .. import notify, repo, reports, views
from ..config import Settings
from ..db import CATEGORIES, STATUSES, Ticket
from ..repo import Viewer
from ..mentor_refs import MAX_IMPORT_BYTES, MAX_MENTOR_REFS, TooManyMentorRefs, parse_mentor_refs
from ..texts import menu_labels, t

router = Router(name="staff")
PAGE_SIZE = 8
_TICKET_RE = re.compile(r"^\s*(?:HM-?)?0*(\d{1,9})\s*$", re.IGNORECASE)


class StaffInput(StatesGroup):
    reply = State()
    note = State()


# ---------- общие помощники ----------

async def _names(sm: async_sessionmaker, tickets: list[Ticket]) -> dict:
    ids = {i for tk in tickets for i in (tk.assigned_to, tk.mentor_id) if i}
    async with sm() as session:
        return await repo.users_by_ids(session, ids)


async def _load(sm: async_sessionmaker, viewer: Viewer, raw_id: str) -> Ticket | None:
    """Тикет, если он существует и этот сотрудник вправе его видеть."""
    if not viewer.is_staff or not raw_id.isdigit():
        return None
    async with sm() as session:
        ticket = await repo.get_ticket(session, int(raw_id))
    return ticket if ticket and repo.can_view(viewer, ticket) else None


async def _edit_or_send(callback: CallbackQuery, text: str, reply_markup) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc).lower():
            await callback.message.answer(text, reply_markup=reply_markup)
    except AttributeError:
        pass


async def _card(sm: async_sessionmaker, viewer: Viewer, ticket: Ticket):
    async with sm() as session:
        notes = await repo.list_notes(session, ticket.id)
    names = await _names(sm, [ticket])
    return (
        views.staff_card(ticket, notes, viewer.lang, names),
        views.staff_card_kb(ticket, viewer.lang, can_assign=viewer.is_admin),
    )


# ---------- очередь ----------

async def _queue(sm: async_sessionmaker, viewer: Viewer, category: str, status: str, page: int):
    async with sm() as session:
        tickets, total = await repo.list_tickets(session, viewer, category, status, page, PAGE_SIZE)
    names = await _names(sm, tickets)
    return (
        views.queue_text(tickets, total, category, status, viewer.lang, names),
        views.queue_kb(tickets, total, category, status, page, PAGE_SIZE, viewer.lang),
    )


@router.message(Command("queue"))
@router.message(F.text.in_(menu_labels("queue")))
async def cmd_queue(message: Message, state: FSMContext, viewer: Viewer, sm: async_sessionmaker) -> None:
    if not viewer.is_staff:
        await message.answer(t(viewer.lang, "staff_only"))
        return
    await state.clear()
    text, kb = await _queue(sm, viewer, "all", "open", 0)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("q:"))
async def cb_queue(callback: CallbackQuery, viewer: Viewer, sm: async_sessionmaker) -> None:
    parts = callback.data.split(":")
    if not viewer.is_staff or len(parts) != 4:
        await callback.answer(t(viewer.lang, "staff_only"), show_alert=True)
        return
    category = parts[1] if parts[1] in CATEGORIES else "all"
    status = parts[2] if parts[2] in STATUSES + ("open", "all") else "open"
    page = int(parts[3]) if parts[3].isdigit() else 0
    text, kb = await _queue(sm, viewer, category, status, page)
    await callback.answer()
    await _edit_or_send(callback, text, kb)


# ---------- карточка ----------

@router.message(Command("ticket"))
async def cmd_ticket(message: Message, command: CommandObject, viewer: Viewer, sm: async_sessionmaker) -> None:
    if not viewer.is_staff:
        await message.answer(t(viewer.lang, "staff_only"))
        return
    match = _TICKET_RE.match(command.args or "")
    if not match:
        await message.answer(t(viewer.lang, "ticket_usage"))
        return
    ticket = await _load(sm, viewer, match.group(1))
    if ticket is None:
        await message.answer(t(viewer.lang, "no_access"))
        return
    text, kb = await _card(sm, viewer, ticket)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("tk:"))
async def cb_ticket(callback: CallbackQuery, viewer: Viewer, sm: async_sessionmaker) -> None:
    ticket = await _load(sm, viewer, callback.data.split(":")[1])
    if ticket is None:
        await callback.answer(t(viewer.lang, "no_access"), show_alert=True)
        return
    text, kb = await _card(sm, viewer, ticket)
    await callback.answer()
    await _edit_or_send(callback, text, kb)


@router.callback_query(F.data.startswith("st:"))
async def cb_status(callback: CallbackQuery, viewer: Viewer, sm: async_sessionmaker, bot: Bot) -> None:
    parts = callback.data.split(":")
    ticket = await _load(sm, viewer, parts[1]) if len(parts) == 3 else None
    if ticket is None or parts[2] not in STATUSES:
        await callback.answer(t(viewer.lang, "no_access"), show_alert=True)
        return
    if ticket.status != parts[2]:
        async with sm() as session:
            ticket = await repo.get_ticket(session, ticket.id)
            await repo.set_status(session, ticket, parts[2])
        await notify.notify_status_change(bot, sm, ticket)
    text, kb = await _card(sm, viewer, ticket)
    await callback.answer()
    await _edit_or_send(callback, text, kb)


@router.callback_query(F.data.startswith("at:"))
async def cb_attachment(callback: CallbackQuery, viewer: Viewer, sm: async_sessionmaker, bot: Bot) -> None:
    ticket = await _load(sm, viewer, callback.data.split(":")[1])
    if ticket is None:
        await callback.answer(t(viewer.lang, "no_access"), show_alert=True)
        return
    if not ticket.attachment_file_id:
        await callback.answer(t(viewer.lang, "no_attachment"), show_alert=True)
        return
    await callback.answer()
    # Отправляем по file_id, а не пересылкой: пересылка раскрыла бы автора.
    sender = {"photo": bot.send_photo, "video": bot.send_video}.get(ticket.attachment_type, bot.send_document)
    try:
        await sender(viewer.id, ticket.attachment_file_id, caption=f"📎 {ticket.code}")
    except TelegramAPIError:
        await callback.message.answer(t(viewer.lang, "no_attachment"))


# ---------- ответ и внутренняя заметка ----------

@router.callback_query(F.data.startswith("rp:") | F.data.startswith("nt:"))
async def cb_ask_text(callback: CallbackQuery, state: FSMContext, viewer: Viewer, sm: async_sessionmaker) -> None:
    kind, raw_id = callback.data.split(":")[:2]
    ticket = await _load(sm, viewer, raw_id)
    if ticket is None:
        await callback.answer(t(viewer.lang, "no_access"), show_alert=True)
        return
    await callback.answer()
    await state.set_state(StaffInput.reply if kind == "rp" else StaffInput.note)
    await state.update_data(ticket_id=ticket.id)
    if kind == "nt":
        key = "ask_note"
    else:
        key = "ask_reply_anon" if ticket.is_anonymous else "ask_reply"
    await callback.message.answer(t(viewer.lang, key, code=ticket.code))


@router.message(StaffInput.reply, F.text & ~F.text.startswith("/"))
@router.message(StaffInput.note, F.text & ~F.text.startswith("/"))
async def got_staff_text(message: Message, state: FSMContext, viewer: Viewer,
                         sm: async_sessionmaker, bot: Bot) -> None:
    is_reply = await state.get_state() == StaffInput.reply.state
    data = await state.get_data()
    await state.clear()
    ticket = await _load(sm, viewer, str(data.get("ticket_id", "")))
    if ticket is None:
        await message.answer(t(viewer.lang, "no_access"))
        return
    text = views.clip(message.text.strip(), 3000)
    async with sm() as session:
        ticket = await repo.get_ticket(session, ticket.id)
        await repo.add_note(session, ticket, text=text, visible=is_reply,
                            author_kind="staff", author_id=viewer.id, author_name=viewer.name)
    if is_reply:
        delivered = await notify.notify_reply(bot, sm, ticket, text)
        key = {True: "reply_saved_delivered", False: "reply_not_delivered", None: "reply_saved"}[delivered]
    else:
        key = "note_saved"
    await message.answer(t(viewer.lang, key, code=ticket.code))
    card, kb = await _card(sm, viewer, ticket)
    await message.answer(card, reply_markup=kb)


# ---------- назначение ----------

@router.callback_query(F.data.startswith("as:"))
async def cb_assign_menu(callback: CallbackQuery, viewer: Viewer, settings: Settings, sm: async_sessionmaker) -> None:
    parts = callback.data.split(":")
    ticket = await _load(sm, viewer, parts[1])
    page = int(parts[2]) if len(parts) == 3 and parts[2].isdigit() and len(parts[2]) < 10 else 0
    if ticket is None or not viewer.is_admin:
        await callback.answer(t(viewer.lang, "no_access"), show_alert=True)
        return
    async with sm() as session:
        if ticket.lead_only:
            people = list((await repo.users_by_ids(session, settings.lead_ids)).values())
        else:
            people = list((await repo.users_by_ids(session, settings.staff_ids)).values())
            people += [m for m in await repo.list_mentors(session) if m.telegram_id not in settings.staff_ids]
    people.sort(key=lambda user: (user.display_name.casefold(), user.telegram_id))
    await callback.answer()
    await _edit_or_send(callback, t(viewer.lang, "choose_assignee", code=ticket.code),
                        views.assign_kb(ticket, people, viewer.lang, page=page))


@router.callback_query(F.data.startswith("asg:"))
async def cb_assign(callback: CallbackQuery, viewer: Viewer, settings: Settings,
                    sm: async_sessionmaker, bot: Bot) -> None:
    parts = callback.data.split(":")
    ticket = await _load(sm, viewer, parts[1]) if len(parts) == 3 else None
    if (ticket is None or not viewer.is_admin or not parts[2].isdigit()
            or len(parts[2]) > 19 or int(parts[2]) > (1 << 63) - 1):
        await callback.answer(t(viewer.lang, "no_access"), show_alert=True)
        return
    target = int(parts[2]) or None
    if target is not None:
        async with sm() as session:
            user = await repo.get_user(session, target)
            role = await repo.effective_role(session, settings, target) if user else "mentee"
        allowed = role == "lead" if ticket.lead_only else role in ("mentor", "coordinator", "lead")
        if not allowed:  # защита от поддельного callback: тикет нельзя «назначить» постороннему
            await callback.answer(t(viewer.lang, "no_access"), show_alert=True)
            return
    async with sm() as session:
        ticket = await repo.get_ticket(session, ticket.id)
        await repo.assign(session, ticket, target)
    if target and target != viewer.id:
        await notify.notify_assignee(bot, sm, settings, ticket, target)
    text, kb = await _card(sm, viewer, ticket)
    await callback.answer()
    await _edit_or_send(callback, text, kb)


# ---------- менторы ----------

async def _mentors_page(sm: async_sessionmaker, lang: str, page: int = 0):
    async with sm() as session:
        mentors = await repo.list_mentors(session)
    return views.mentors_page(mentors, lang, page)


@router.message(Command("mentors"))
async def cmd_mentors(message: Message, command: CommandObject, viewer: Viewer,
                      sm: async_sessionmaker) -> None:
    if not viewer.is_admin:
        await message.answer(t(viewer.lang, "admin_only"))
        return
    arg = (command.args or "").strip()
    page = max(0, int(arg) - 1) if arg.isdigit() and len(arg) < 10 else 0
    text, kb = await _mentors_page(sm, viewer.lang, page)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("ml:"))
async def cb_mentors_page(callback: CallbackQuery, viewer: Viewer, sm: async_sessionmaker) -> None:
    raw = callback.data.split(":", 1)[1]
    if not viewer.is_admin or not raw.isdigit() or len(raw) > 9:
        await callback.answer(t(viewer.lang, "admin_only"), show_alert=True)
        return
    text, kb = await _mentors_page(sm, viewer.lang, int(raw))
    await callback.answer()
    await _edit_or_send(callback, text, kb)


class _ImportTooLarge(ValueError):
    pass


class _LimitedBuffer(BytesIO):
    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > MAX_IMPORT_BYTES:
            raise _ImportTooLarge
        return super().write(data)


def _user_label(user) -> str:
    handle = f"@{user.username} — " if user.username else ""
    return f"{handle}{user.telegram_id} — {user.display_name}"


def _mentor_report(result: repo.MentorEditResult, lang: str, adding: bool) -> str:
    groups = [
        ("mentor_bulk_added" if adding else "mentor_bulk_removed", [_user_label(u) for u in result.changed]),
        ("mentor_bulk_already" if adding else "mentor_bulk_not_members", [_user_label(u) for u in result.unchanged]),
        ("mentor_bulk_unknown", result.unknown),
        ("mentor_bulk_ambiguous", result.ambiguous),
        ("mentor_bulk_invalid", result.invalid),
    ]
    lines = []
    for key, values in groups:
        lines += [t(lang, key, n=len(values)), *values, ""]
    lines += [t(lang, "mentor_bulk_duplicates", n=result.duplicates)]
    if result.unassigned:
        lines += [t(lang, "mentor_bulk_unassigned", n=result.unassigned)]
    return "\n".join(lines)


@router.message(Command("addmentor", "delmentor", "addmentors", "delmentors", ignore_case=True))
async def cmd_mentor_edit(message: Message, command: CommandObject, viewer: Viewer,
                          settings: Settings, sm: async_sessionmaker, bot: Bot) -> None:
    if not viewer.is_admin:
        await message.answer(t(viewer.lang, "admin_only"))
        return
    lang = viewer.lang
    adding = command.command.lower().startswith("add")
    text = command.args or ""
    document = message.document
    if document is None and message.reply_to_message:
        document = message.reply_to_message.document
    if document:
        filename = (document.file_name or "").lower()
        if not filename.endswith(".txt"):
            await message.answer(t(lang, "mentor_file_format"))
            return
        if document.file_size and document.file_size > MAX_IMPORT_BYTES:
            await message.answer(t(lang, "mentor_file_large"))
            return
        try:
            with _LimitedBuffer() as destination:
                await bot.download(document, destination=destination)
                text += "\n" + destination.getvalue().decode("utf-8-sig")
        except _ImportTooLarge:
            await message.answer(t(lang, "mentor_file_large"))
            return
        except UnicodeError:
            await message.answer(t(lang, "mentor_file_encoding"))
            return
        except (TelegramAPIError, OSError):
            await message.answer(t(lang, "mentor_file_failed"))
            return
    if not text.strip():
        await message.answer(t(lang, "mentor_usage", cmd=command.command))
        return
    try:
        parsed = parse_mentor_refs(text)
    except TooManyMentorRefs:
        await message.answer(t(lang, "mentor_too_many", n=MAX_MENTOR_REFS))
        return
    if not parsed.total:
        await message.answer(t(lang, "mentor_usage", cmd=command.command))
        return
    async with sm() as session:
        result = await repo.edit_mentors(session, parsed, adding=adding, protected_ids=settings.staff_ids)
    lines = [
        t(lang, "mentor_bulk_added" if adding else "mentor_bulk_removed", n=len(result.changed)),
        t(lang, "mentor_bulk_already" if adding else "mentor_bulk_not_members", n=len(result.unchanged)),
        t(lang, "mentor_bulk_unknown", n=len(result.unknown)),
    ]
    for key, values in (("mentor_bulk_ambiguous", result.ambiguous), ("mentor_bulk_invalid", result.invalid)):
        if values:
            lines.append(t(lang, key, n=len(values)))
    if result.duplicates:
        lines.append(t(lang, "mentor_bulk_duplicates", n=result.duplicates))
    if result.unassigned:
        lines.append(t(lang, "mentor_bulk_unassigned", n=result.unassigned))
    # A short error preview; the full report is attached for large/error lists.
    errors = result.unknown + result.ambiguous + result.invalid
    if errors:
        preview = ", ".join(escape(views.clip(ref, 60)) for ref in errors[:5])
        lines += ["", preview, t(lang, "mentor_resolve_hint")]
    if parsed.total > 1:
        lines += ["", t(lang, "mentor_bulk_no_notify")]
    await message.answer("\n".join(lines))
    if document or parsed.total > 1:
        report = BufferedInputFile(_mentor_report(result, lang, adding).encode("utf-8"), filename="mentors_result.txt")
        await message.answer_document(report, caption=t(lang, "mentor_report_file"))
    if parsed.total == 1 and result.changed:
        user = result.changed[0]
        role = settings.role_for(user.telegram_id, is_mentor=adding)
        await notify.safe_send(
            bot, user.telegram_id, t(user.lang, "you_are_mentor" if adding else "you_are_not_mentor"),
            staff=True, reply_markup=views.main_menu(user.lang, role != "mentee"),
        )


# ---------- отчёт по запросу ----------

@router.message(Command("report"))
async def cmd_report(message: Message, viewer: Viewer, sm: async_sessionmaker) -> None:
    if not viewer.is_admin:
        await message.answer(t(viewer.lang, "admin_only"))
        return
    await message.answer(
        await reports.build_weekly_report(sm, viewer.lang, include_lead_only=viewer.role == "lead")
    )
