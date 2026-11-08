"""Инструменты команды программы (раздел 6.6): очередь, карточка, статусы, ответы,
заметки, назначение, менторы, отчёт. Всё внутри Telegram — таблица не нужна."""
from __future__ import annotations

import re
from dataclasses import replace
from html import escape
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .. import clock, dashboard, notify, repo, reports, views
from ..config import Settings
from ..db import CATEGORIES, Mentor, MentorRole, STATUSES, Subject, Term, Ticket, User
from ..mentoring import service as svc
from ..mentoring.ui import e
from ..repo import Viewer
from ..mentor_refs import MAX_IMPORT_BYTES, MAX_MENTOR_REFS, TooManyMentorRefs, parse_mentor_refs
from ..texts import menu_labels, t

router = Router(name="staff")
PAGE_SIZE = 8
_TICKET_RE = re.compile(r"^\s*(?:HM-?)?0*(\d{1,9})\s*$", re.IGNORECASE)


class StaffInput(StatesGroup):
    reply = State()
    note = State()


class FinalForm(StatesGroup):
    """Итоговый отчёт ментора — Приложение 4: три текстовых поля."""
    summary = State()
    difficulties = State()
    proposals = State()


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


# ---------- триместр (п. 8.1.5) ----------

@router.message(Command("term"))
async def cmd_term(message: Message, command: CommandObject, viewer: Viewer,
                   settings: Settings, sm: async_sessionmaker) -> None:
    if not viewer.is_admin:
        await message.answer(t(viewer.lang, "admin_only"))
        return
    args = (command.args or "").split()
    async with sm() as s:
        if not args:
            term, week = await svc.week_now(s, settings)
            if term is None:
                await message.answer(
                    "Триместр не задан.\n\n"
                    "Пока его нет, бот не знает номер недели, не действует правило "
                    "«меньше 2 записей → отмена» (п. 8.1.3) и не работает исключение недель 5/9/10.\n\n"
                    "<b>Задать:</b> <code>/term осень26 2026-09-07 12</code>\n"
                    "Аргументы: название, понедельник первой недели, число недель (обычно 12).")
                return
            await s.commit()
            await message.answer(
                f"<b>Триместр «{term.name}»</b>\n"
                f"Старт: {term.starts_on} (пн), недель: {term.weeks}, поток: {term.cohort}\n"
                f"Сейчас: <b>неделя {week}</b>"
                + ("\nПиковая неделя: слоты не отменяются автоматически (п. 8.1.3)."
                   if svc.is_peak_week(week) else "")
                + "\n\nОриентиры: 5 — мидтермы, 10 — эндтермы, 11 — финальные экзамены, 12 — пересдача.")
            return
        if len(args) not in (3, 4):
            await message.answer("Формат: <code>/term осень26 2026-09-07 12 2500</code>\n"
                                 "Название, понедельник старта, число недель, размер потока (необязательно).")
            return
        try:
            weeks = int(args[2])
            cohort = int(args[3]) if len(args) == 4 else 2500
        except ValueError:
            await message.answer("Число недель и размер потока должны быть числами, например 12 и 2500.")
            return
        try:
            term = await svc.create_term(s, viewer, args[0], args[1], weeks, cohort)
        except svc.RuleError as exc:
            await message.answer(str(exc))
            return
        await s.commit()
    await message.answer(f"✅ Триместр «{term.name}» задан: с {term.starts_on}, {term.weeks} недель.")


# ---------- роли менторов по предметам (п. 5.9) ----------

ROLE_ALIASES = {
    "lead": "lead", "ведущий": "lead",
    "respondent": "respondent", "респондент": "respondent",
    "author": "author", "автор": "author",
}


async def _find_user(s, ref: str) -> User | None:
    ref = ref.strip()
    if ref.startswith("@"):
        return await s.scalar(select(User).where(User.username == ref[1:]))
    if ref.isdigit():
        return await s.get(User, int(ref))
    return await s.scalar(select(User).where(User.username == ref))


@router.message(Command("role", "roles"))
async def cmd_role(message: Message, command: CommandObject, viewer: Viewer,
                   settings: Settings, sm: async_sessionmaker) -> None:
    if not viewer.is_admin:
        await message.answer(t(viewer.lang, "admin_only"))
        return
    args = (command.args or "").strip()
    if not args:
        # Кнопочный путь: /role — назначить, /roles — посмотреть.
        if command.command.lower() == "roles":
            await _roles_home(message, settings, sm)
        else:
            await _mentor_pick(message, settings, sm, 0)
        return
    await _role_by_text(message, args, viewer, settings, sm)


async def _role_by_text(message: Message, args: str, viewer: Viewer, settings: Settings,
                        sm: async_sessionmaker) -> None:
    """Текстовая форма остаётся для скриптов: /role @user ведущий Calculus 1."""
    parts = args.split(maxsplit=2)
    if len(parts) != 3:
        await message.answer("Формат: <code>/role @user ведущий Calculus 1</code>\n"
                             "Или без аргументов — <code>/role</code> и кнопки.")
        return
    ref, role_raw, subject_raw = parts
    role = ROLE_ALIASES.get(role_raw.casefold())
    if role is None:
        await message.answer("Роль: ведущий, респондент или автор.")
        return
    async with sm() as s:
        term = await svc.active_term(s, settings)
        if term is None:
            await message.answer("Сначала задайте триместр: <code>/term осень26 2026-09-07 12</code>")
            return
        user = await _find_user(s, ref)
        if user is None:
            await message.answer(f"Не нашёл пользователя «{ref}». Нужен @ник или numeric id.")
            return
        subject_key = " ".join(subject_raw.split()).casefold()
        subject = await s.scalar(select(Subject).where(Subject.name_key == subject_key))
        if subject is None:
            await message.answer(f"Предмет «{subject_raw}» не найден. Он появляется после первого занятия по нему.")
            return
        try:
            await svc.set_mentor_role(s, viewer, user.telegram_id, subject.id, term.id, role)
            await s.commit()
        except svc.RuleError as exc:
            await message.answer(str(exc))
            return
    await message.answer(f"✅ {_label(user)} — {svc.ROLE_LABELS[role]} · {subject.name} · {term.name}")


# ---- экраны ролей ----

def _label(user) -> str:
    return f"@{user.username}" if getattr(user, "username", None) else str(user.telegram_id)


async def _mentor_page(s, page: int, size: int = 8) -> tuple[list, int]:
    ids = list(await s.scalars(select(Mentor.telegram_id)))
    if not ids:
        return [], 0
    users = list(await s.scalars(select(User).where(User.telegram_id.in_(ids))
                                 .order_by(User.display_name)))
    page = max(page, 0)
    return users[page * size:(page + 1) * size], len(users)


def _nav(page: int, total: int, size: int, prefix: str) -> list:
    rows = []
    buttons = []
    if page > 0:
        buttons.append(_b("← Назад", f"{prefix}{page - 1}"))
    if (page + 1) * size < total:
        buttons.append(_b("Вперёд →", f"{prefix}{page + 1}"))
    if buttons:
        rows.append(buttons)
    return rows


async def _no_term(target) -> None:
    text = ("Сначала задайте триместр — роли фиксируются на триместр (п. 5.9).\n\n"
            "<code>/term осень26 2026-09-07 12</code>")
    if hasattr(target, "data"):
        await _edit_or_send(target, text, None)
    else:
        await target.answer(text)


async def _mentor_pick(target, settings: Settings, sm: async_sessionmaker, page: int) -> None:
    async with sm() as s:
        if await svc.active_term(s, settings) is None:
            await _no_term(target)
            return
        users, total = await _mentor_page(s, page)
    if not users:
        await _send(target, "В реестре нет менторов. Добавьте: <code>/addmentor @ник</code>",
                    None)
        return
    rows = [[_b(_label(u), f"rl:men:{u.telegram_id}")] for u in users]
    rows += _nav(page, total, 8, "rl:pick:")
    rows.append([_b("← Меню ролей", "rl:view")])
    await _send(target, "<b>Кому назначить роль?</b>\n\nВыберите ментора.", _markup(*rows))


async def _roles_home(target, settings: Settings, sm: async_sessionmaker) -> None:
    async with sm() as s:
        term = await svc.active_term(s, settings)
    head = f"<b>Роли · {term.name}</b>" if term else "<b>Роли · триместр не задан</b>"
    await _send(target, f"{head}\n\nКак смотреть?", _markup(
        [_b("По предметам", "rl:sub")],
        [_b("По людям", "rl:ppl:0")],
        [_b("Назначить роль", "rl:pick:0")]))


async def _subject_roles(target, settings: Settings, sm: async_sessionmaker, subject_id: int) -> None:
    async with sm() as s:
        term = await svc.active_term(s, settings)
        if term is None:
            await _no_term(target)
            return
        subject = await s.get(Subject, subject_id)
        if subject is None:
            await _send(target, "Предмет не найден.", None)
            return
        rows = (await s.execute(
            select(User, MentorRole.role)
            .join(MentorRole, MentorRole.telegram_id == User.telegram_id)
            .where(MentorRole.subject_id == subject_id, MentorRole.term_id == term.id)
            .order_by(MentorRole.role, User.display_name))).all()
    lines = [f"<b>{subject.name} · {term.name}</b>", ""]
    for role in ("lead", "respondent", "author"):
        people = [_label(u) for u, r in rows if r == role]
        lines.append(f"{svc.ROLE_LABELS[role]}: " + (", ".join(people) if people else "—"))
    await _send(target, "\n".join(lines), _markup(
        [_b("◀ К предметам", "rl:sub")], [_b("← Меню ролей", "rl:view")]))


async def _person_roles(target, settings: Settings, sm: async_sessionmaker, uid: int) -> None:
    async with sm() as s:
        term = await svc.active_term(s, settings)
        if term is None:
            await _no_term(target)
            return
        user = await s.get(User, uid)
        pairs = await svc.roles_of(s, uid, term.id)
    lines = [f"<b>{_label(user) if user else uid} · {term.name}</b>", ""]
    if not pairs:
        lines.append("Ролей пока нет.")
    for subject, role in pairs:
        lines.append(f"· {svc.ROLE_LABELS[role]} — {subject}")
    await _send(target, "\n".join(lines), _markup(
        [_b("◀ К людям", "rl:ppl:0")], [_b("← Меню ролей", "rl:view")]))


@router.callback_query(F.data.startswith("rl:"))
async def cb_roles(callback: CallbackQuery, viewer: Viewer, settings: Settings,
                   sm: async_sessionmaker) -> None:
    if not viewer.is_admin:
        await callback.answer(t(viewer.lang, "admin_only"), show_alert=True)
        return
    parts = callback.data.split(":")
    op = parts[1]
    try:
        if op == "view":
            await _roles_home(callback, settings, sm)
        elif op == "pick":
            await _mentor_pick(callback, settings, sm, int(parts[2]))
        elif op == "sub":
            async with sm() as s:
                subjects = list(await s.scalars(select(Subject).order_by(Subject.name_key)))
            if not subjects:
                await _send(callback, "Предметов ещё нет: они появляются после первого занятия.",
                            _markup([_b("← Меню ролей", "rl:view")]))
                return
            rows = [[_b(s_.name, f"rl:subv:{s_.id}")] for s_ in subjects]
            rows.append([_b("← Меню ролей", "rl:view")])
            await _send(callback, "<b>Роли по предметам</b>\n\nВыберите предмет.", _markup(*rows))
        elif op == "subv":
            await _subject_roles(callback, settings, sm, int(parts[2]))
        elif op == "ppl":
            await _people(callback, settings, sm, int(parts[2]))
        elif op == "prs":
            await _person_roles(callback, settings, sm, int(parts[2]))
        elif op == "men":
            await _subject_pick(callback, sm, int(parts[2]))
        elif op == "sid":
            await _role_pick(callback, sm, int(parts[2]), int(parts[3]))
        elif op == "set":
            await _assign(callback, viewer, settings, sm, int(parts[2]), int(parts[3]), parts[4])
        await callback.answer()
    except svc.RuleError as exc:
        await callback.answer(str(exc), show_alert=True)


async def _people(target, settings: Settings, sm: async_sessionmaker, page: int) -> None:
    async with sm() as s:
        users, total = await _mentor_page(s, page)
    if not users:
        await _send(target, "В реестре нет менторов.", _markup([_b("← Меню ролей", "rl:view")]))
        return
    rows = [[_b(_label(u), f"rl:prs:{u.telegram_id}")] for u in users]
    rows += _nav(page, total, 8, "rl:ppl:")
    rows.append([_b("← Меню ролей", "rl:view")])
    await _send(target, "<b>Роли по людям</b>\n\nЧей профиль открыть?", _markup(*rows))


async def _subject_pick(target, sm: async_sessionmaker, uid: int) -> None:
    async with sm() as s:
        subjects = list(await s.scalars(select(Subject).order_by(Subject.name_key)))
        user = await s.get(User, uid)
    if not subjects:
        await _send(target, "Предметов ещё нет.", _markup([_b("← Меню ролей", "rl:view")]))
        return
    rows = [[_b(s_.name, f"rl:sid:{uid}:{s_.id}")] for s_ in subjects]
    rows.append([_b("← Другой ментор", "rl:pick:0")])
    await _send(target, f"<b>Роль для {_label(user) if user else uid}</b>\n\nВыберите предмет.",
                _markup(*rows))


async def _role_pick(target, sm: async_sessionmaker, uid: int, subject_id: int) -> None:
    async with sm() as s:
        subject = await s.get(Subject, subject_id)
        user = await s.get(User, uid)
    rows = [[_b(f"{svc.ROLE_LABELS[r]}", f"rl:set:{uid}:{subject_id}:{r}")]
            for r in ("lead", "respondent", "author")]
    rows.append([_b("← Другой предмет", f"rl:men:{uid}")])
    await _send(target, f"<b>{_label(user) if user else uid} · {subject.name if subject else subject_id}</b>\n\n"
                        "Какая роль?", _markup(*rows))


async def _assign(target, viewer: Viewer, settings: Settings, sm: async_sessionmaker,
                  uid: int, subject_id: int, role: str) -> None:
    async with sm() as s:
        term = await svc.active_term(s, settings)
        if term is None:
            await _no_term(target)
            return
        await svc.set_mentor_role(s, viewer, uid, subject_id, term.id, role)
        await s.commit()
        user = await s.get(User, uid)
        subject = await s.get(Subject, subject_id)
    await _send(target, f"✅ {_label(user) if user else uid} — {svc.ROLE_LABELS[role]} · "
                        f"{subject.name if subject else subject_id} · {term.name}",
                _markup([_b("Ещё роль этому ментору", f"rl:men:{uid}")],
                        [_b("← Меню ролей", "rl:view")]))


def _b(label: str, data: str):
    return (label, data)


def _markup(*rows):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in rows])


async def _send(target, text: str, markup) -> None:
    """Одна точка вывода: и для Message, и для CallbackQuery редактируем на месте."""
    if hasattr(target, "data"):  # CallbackQuery
        await _edit_or_send(target, text[:4096], markup)
    else:
        await target.answer(text[:4096], reply_markup=markup)


# ---------- KPI ментора (раздел 11) ----------

def _kpi_block(kpi: dict) -> str:
    fb = kpi['feedback']
    lines = [f"Проведено менторок: <b>{kpi['completed']}</b> из {kpi['lessons']} запланированных"
             + (f" · отменено {kpi['cancelled']}" if kpi['cancelled'] else ""),
             f"Записей студентов: <b>{kpi['bookings']}</b>"
             f" · уникальных студентов: <b>{kpi['unique_students']}</b>"
             f" · пришло на занятия: {kpi['present']}",
             f"Материалов загружено в Банк: <b>{kpi['materials']}</b>",
             f"Недельных отчётов: {kpi['reports_on_time']} вовремя"
             + (f", {kpi['reports_late']} с опозданием" if kpi['reports_late'] else ""),
             ]
    if fb['count']:
        lines.append(f"Оценки студентов: <b>{score_of(fb)}</b> из 5 по {fb['count']} отзывам")
        lines.append(f"   · письменных отзывов: {fb.get('written', 0)}")
        lines.append(f"   · доступность {fb['availability']} · компетентность {fb['competence']}"
                     f" · этика {fb['ethics']}")
        if fb['violations']:
            lines.append(f"   ⚠️ отметок «дал готовое решение»: {fb['violations']}")
    else:
        lines.append("Оценки студентов: пока нет отзывов")
    lines.append(f"Статус по Положению: <b>{svc.kpi_grade(kpi)}</b>")
    return "\n".join(lines)


def score_of(fb: dict):
    return fb['score']


KPI_PAGE = 10


async def _kpi_page(s, settings: Settings, page: int) -> tuple[str, object]:
    """KPI постранично: на 30 менторах один экран — это стена текста."""
    term, week = await svc.week_now(s, settings)
    if term is None:
        return "KPI · триместр не задан\n\nЗадайте: <code>/term осень26 2026-09-07 12 2500</code>", _markup()
    mentors = list(await s.scalars(select(Mentor.telegram_id).order_by(Mentor.telegram_id)))
    users = {u.telegram_id: u for u in await s.scalars(select(User).where(User.telegram_id.in_(mentors)))} \
        if mentors else {}
    pages = max(1, (len(mentors) + KPI_PAGE - 1) // KPI_PAGE)
    page = min(max(page, 0), pages - 1)
    chunk = mentors[page * KPI_PAGE:(page + 1) * KPI_PAGE]
    lines = [f"<b>KPI · {e(term.name, 40)}, неделя {week}</b>",
             f"Менторы {page * KPI_PAGE + 1}–{page * KPI_PAGE + len(chunk)} из {len(mentors)}", ""]
    for mentor_id in chunk:
        kpi = await svc.mentor_kpi(s, mentor_id, settings)
        user = users.get(mentor_id)
        label = (user.username and f"@{user.username}") or str(mentor_id)
        fb = kpi['feedback']
        lines.append(f"· {label}: менторки {kpi['completed']}/{kpi['lessons']}"
                     f" · записи {kpi['bookings']}"
                     f" · материалы {kpi['materials']}"
                     f" · оценки {fb['score'] if fb['count'] else '—'}")
    if not mentors:
        lines.append("В реестре нет менторов.")
    rows = []
    nav = []
    if page > 0:
        nav.append(_b("← Назад", f"kpi:{page - 1}"))
    if page + 1 < pages:
        nav.append(_b("Вперёд →", f"kpi:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_b("← Менторки", "ms:home")])
    return "\n".join(lines), _markup(*rows)


@router.message(Command("kpi"))
async def cmd_kpi(message: Message, viewer: Viewer, settings: Settings,
                  sm: async_sessionmaker) -> None:
    if not viewer.is_staff:
        await message.answer(t(viewer.lang, "staff_only"))
        return
    async with sm() as s:
        if not viewer.is_admin:
            kpi = await svc.mentor_kpi(s, viewer.id, settings)
            term, week = await svc.week_now(s, settings)
            await s.commit()
            head = (f"<b>Мой KPI</b> · {term.name}, неделя {week}" if term else "<b>Мой KPI</b>")
            await message.answer(f"{head}\n\n{_kpi_block(kpi)}")
            return
        text, markup = await _kpi_page(s, settings, 0)
        await s.commit()
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("kpi:"))
async def cb_kpi(callback: CallbackQuery, viewer: Viewer, settings: Settings,
                 sm: async_sessionmaker) -> None:
    if not viewer.is_admin:
        await callback.answer(t(viewer.lang, "admin_only"), show_alert=True)
        return
    page = int(callback.data.split(":")[1])
    async with sm() as s:
        text, markup = await _kpi_page(s, settings, page)
        await s.commit()
    await _edit_or_send(callback, text[:4096], markup)
    await callback.answer()


# ---------- сводка для администрации ----------

def _dash_keyboard(section=None, *, block="overview", subject_id=None, lesson_id=None,
                   lesson=None, ctx=None):
    ctx = ctx or dashboard.DashContext(block=block, subject_id=subject_id or 0)
    if section is not None:
        ctx = section.get('ctx', ctx)
    rows = []
    if section is not None:
        rows.extend([[_b(label, target)] for label, target in section['links']])
        nav = []
        if section['page']:
            nav.append(_b('← Назад', dashboard.list_cb(
                block, replace(ctx, page=section['page'] - 1))))
        if section['page'] + 1 < section['pages']:
            nav.append(_b('Далее →', dashboard.list_cb(
                block, replace(ctx, page=section['page'] + 1))))
        if nav:
            rows.append(nav)
        if block in dashboard.FILTER_BLOCKS:
            rows.append([_b('⚙ Фильтры', dashboard.filters_cb(block, ctx))])
            if dashboard.has_filters(ctx):
                rows.append([_b('✖ Сбросить фильтры', dashboard.reset_cb(block, ctx))])
    if lesson_id is not None:
        rows.append([_b('← Назад', dashboard.list_cb(ctx.block, ctx))])
        if lesson is not None and lesson.format_confirmed is not True and lesson.status in ('scheduled', 'completed'):
            label = '✅ Указать формат' if lesson.status == 'completed' else '✅ Подтвердить формат'
            rows.append([_b(label, dashboard.confirm_cb(lesson_id, ctx))])
        rows.append([_b('👥 Участники', f'ms:people:{lesson_id}:0')])
        if lesson is not None and lesson.status != 'cancelled':
            rows.append([_b('📎 Материалы занятия', f'ms:upload:{lesson_id}')])
            # Назначить Автора, сменить Ведущего и т. п. — без записи администратора студентом.
            rows.append([_b('⚙️ Управление занятием', f'ms:manage:{lesson_id}')])
        rows.append([_b('← Все занятия', 'dash:sessions')])
    if block == 'overview':
        rows.extend([
            [_b('Занятия недели', 'dash:week'), _b('Требует внимания', 'dash:attention')],
            [_b('Все занятия', 'dash:sessions'), _b('Неподтверждённые форматы', 'dash:unconfirmed')],
            [_b('Цикл по предметам', 'dash:cycle'), _b('Люди и отчёты', 'dash:people')],
            [_b('Долги по занятиям', 'dash:debts'), _b('KPI и цели', 'dash:kpi')],
            [_b('Обращения', 'dash:tickets'), _b('Выгрузить CSV', 'dash:csv')],
            [_b('Обновить сводку', 'dash:overview')]])
    else:
        rows.append([_b('← Сводка', 'dash:overview')])
    rows.append([_b('← Главное меню менторок', 'ms:home')])
    return _markup(*rows)


def _dash_filter_keyboard(data, block, ctx):
    rows = []
    periods = list(dashboard.PERIOD_LABELS.items())
    for i in range(0, len(periods), 3):
        rows.append([_b(label, dashboard.setf_cb('period', key, block,
                                                replace(ctx, page=0)))
                     for key, label in periods[i:i + 3]])
    if block != 'subject':
        rows.append([_b(f"Предмет: {dashboard.subject_label(data, ctx.subject_id)}",
                        dashboard.pick_cb('subject', block, ctx))])
    rows.append([_b(f"Ведущий: {dashboard.mentor_label(data, ctx.mentor_id)}",
                    dashboard.pick_cb('mentor', block, ctx))])
    rows.append([_b('✖ Сбросить фильтры', dashboard.reset_cb(block, ctx))])
    rows.append([_b('← К списку', dashboard.list_cb(block, ctx))])
    return _markup(*rows)


def _dash_picker_keyboard(data, field, block, ctx, picker_page):
    options = dashboard.picker_options(data, field)
    items, page, pages = dashboard.page_slice(options, picker_page)
    rows = [[_b(label, dashboard.pickv_cb(field, value, block,
                                         replace(ctx, page=0)))] for value, label in items]
    all_label = 'Все предметы' if field == 'subject' else 'Все ведущие'
    rows.append([_b(all_label, dashboard.pickv_cb(field, 0, block, replace(ctx, page=0)))])
    nav = []
    if page:
        nav.append(_b('← Назад', dashboard.pick_cb(field, block, ctx, page - 1)))
    if page + 1 < pages:
        nav.append(_b('Далее →', dashboard.pick_cb(field, block, ctx, page + 1)))
    if nav:
        rows.append(nav)
    rows.append([_b('← Фильтры', dashboard.filters_cb(block, ctx))])
    return _markup(*rows)


async def _dash_show_filters(callback, data, block, ctx):
    await _edit_or_send(callback, dashboard.filter_panel_text(data, ctx),
                        _dash_filter_keyboard(data, block, ctx))


async def _dash_show_section(callback, data, block, page, ctx):
    section = dashboard.section_page(data, block, page, ctx=ctx)
    await _edit_or_send(callback, section['text'], _dash_keyboard(
        section, block=block, ctx=section['ctx']))


async def _dash_show_lesson(callback, data, lesson, ctx):
    await _edit_or_send(callback, dashboard.lesson_text(data, lesson), _dash_keyboard(
        block='lesson', lesson_id=lesson.id, lesson=lesson, ctx=ctx))


def _dash_bad_context(callback, message='Исходный раздел больше недоступен.'):
    markup = _markup([_b('Все занятия', 'dash:sessions')],
                     [_b('← Сводка', 'dash:overview')])
    return _edit_or_send(callback, message + '\nВыберите безопасный путь ниже.', markup)


@router.message(Command("dashboard", "svodka"))
async def cmd_dashboard(message: Message, viewer: Viewer, settings: Settings,
                        sm: async_sessionmaker) -> None:
    if not viewer.is_admin:
        await message.answer(t(viewer.lang, "admin_only"))
        return
    if message.chat.type != 'private':
        await message.answer('Сводка доступна в личном чате с ботом. Откройте там /dashboard.')
        return
    async with sm() as s:
        data = await dashboard.overview(s, settings)
    await message.answer(dashboard.overview_text(data), reply_markup=_dash_keyboard())


@router.callback_query(F.data.startswith("dash:"))
async def cb_dashboard(callback: CallbackQuery, viewer: Viewer, settings: Settings,
                       sm: async_sessionmaker) -> None:
    if not viewer.is_admin:
        await callback.answer(t(viewer.lang, "admin_only"), show_alert=True)
        return
    if callback.message is None or callback.message.chat.type != 'private':
        await callback.answer('Откройте /dashboard в личном чате с ботом.', show_alert=True)
        return
    parts = callback.data.split(':')
    action = parts[1]
    paged = {'week', 'sessions', 'cycle', 'people', 'debts', 'attention', 'subject', 'unconfirmed'}
    page, subject_id, lesson_id = 0, None, None
    ctx = dashboard.DashContext(block=action)
    block = action

    def _int_part(index, default=0, limit=10):
        if len(parts) <= index:
            return default
        if not parts[index].isdigit() or len(parts[index]) > limit:
            return None
        return int(parts[index])

    def _code_block(index):
        return dashboard.CODE_BLOCKS.get(parts[index]) if len(parts) > index else None

    if action in ('filters', 'setf', 'pick', 'pickv', 'confirm', 'previewfmt', 'setfmt'):
        # Разбираем фильтры/подтверждение формата отдельно: у них свой хвост.
        offset = {'filters': 5, 'setf': 7, 'pick': 6, 'pickv': 7,
                  'confirm': 5, 'previewfmt': 6, 'setfmt': 6}[action]
        code_index = {'filters': 2, 'setf': 4, 'pick': 3, 'pickv': 4,
                      'confirm': 3, 'previewfmt': 4, 'setfmt': 4}[action]
        anchor_index = {'filters': 3, 'setf': 5, 'pick': 4, 'pickv': 5,
                        'confirm': None, 'previewfmt': None, 'setfmt': None}[action]
        page_index = {'filters': 4, 'setf': 6, 'pick': 5, 'pickv': 6,
                      'confirm': 4, 'previewfmt': 5, 'setfmt': 5}[action]
        block = _code_block(code_index)
        page = _int_part(page_index, 0, 6)
        if block is None or page is None:
            await callback.answer('Ссылка устарела. Откройте список заново.', show_alert=True)
            return
        anchor = _int_part(anchor_index, 0, 10) if anchor_index is not None else 0
        if anchor is None:
            await callback.answer('Ссылка устарела. Откройте список заново.', show_alert=True)
            return
        ctx = dashboard.ctx_from_parts(parts, offset, block=block, page=page)
        if block == 'subject':
            ctx = replace(ctx, subject_id=anchor)
        async with sm() as s:
            data = await dashboard.overview(s, settings)
            if action == 'filters':
                text = dashboard.filter_panel_text(data, ctx)
                markup = _dash_filter_keyboard(data, block, ctx)
            elif action == 'pick':
                field = parts[2]
                picker_page = _int_part(len(parts) - 1, 0, 4) if len(parts) > offset else 0
                if field not in ('subject', 'mentor') or picker_page is None:
                    await callback.answer('Фильтр не найден.', show_alert=True)
                    return
                text = dashboard.picker_text(data, field, ctx, picker_page)
                markup = _dash_picker_keyboard(data, field, block, ctx, picker_page)
            elif action == 'pickv':
                field = parts[2]
                value = _int_part(3, 0, 10)
                if field not in ('subject', 'mentor') or value is None:
                    await callback.answer('Фильтр не найден.', show_alert=True)
                    return
                new_ctx = replace(ctx, page=0,
                                  subject_id=value if field == 'subject' else ctx.subject_id,
                                  mentor_id=value if field == 'mentor' else ctx.mentor_id)
                await _dash_show_filters(callback, data, block, new_ctx)
                await callback.answer()
                return
            elif action == 'setf':
                field, value = parts[2], parts[3]
                if field != 'period' or value not in dashboard.PERIOD_LABELS:
                    await callback.answer('Фильтр не найден.', show_alert=True)
                    return
                new_ctx = replace(ctx, page=0, period=value)
                await _dash_show_filters(callback, data, block, new_ctx)
                await callback.answer()
                return
            else:  # confirm / previewfmt / setfmt
                lesson_id = _int_part(2, 0, 10)
                if lesson_id is None:
                    await callback.answer('Занятие не найдено.', show_alert=True)
                    return
                lesson = next((x for x in data['lessons'] if x.id == lesson_id), None)
                if lesson is None:
                    await _dash_bad_context(callback, 'Занятие не найдено в текущем срезе сводки.')
                    return
                if action == 'confirm':
                    if lesson.status == 'cancelled':
                        await _dash_bad_context(
                            callback, 'Формат отменённого занятия не исправляется.')
                        return
                    if lesson.status not in ('scheduled', 'completed'):
                        await _dash_bad_context(callback, 'Формат этого занятия изменить нельзя.')
                        return
                    rows = [[_b(label, dashboard.previewfmt_cb(lesson_id, key, ctx))]
                            for key, label in svc.FORMAT_LABELS.items()]
                    rows.append([_b('← К занятию', dashboard.lesson_cb(lesson_id, ctx))])
                    text = ('Выберите фактический формат занятия.\n'
                            'Сначала будет предпросмотр; без явного подтверждения ничего не меняется.')
                    markup = _markup(*rows)
                elif action == 'previewfmt':
                    fmt = parts[3]
                    if fmt not in svc.FORMAT_LABELS:
                        await callback.answer('Неизвестный формат.', show_alert=True)
                        return
                    old = svc.FORMAT_LABELS.get(lesson.format, '[__]')
                    old_state = 'подтверждён' if lesson.format_confirmed else 'не подтверждён'
                    text = '\n'.join([
                        f'<b>Подтверждение формата · занятие #{lesson_id}</b>',
                        f'Занятие: {lesson.status.replace("scheduled", "в расписании").replace("completed", "проведено")}',
                        f'Было: {e(old, 40)} · {old_state}',
                        f'Станет: {e(svc.FORMAT_LABELS[fmt], 40)} · подтверждён',
                        '',
                        'После сохранения занятие будет считаться по фактическому формату; '
                        'это может изменить охват Syllabus Clinic и другие показатели.',
                        'Другие занятия не меняются.',
                    ])
                    rows = [[_b('✅ Подтвердить', dashboard.setfmt_cb(lesson_id, fmt, ctx))],
                            [_b('← Отмена', dashboard.lesson_cb(lesson_id, ctx))]]
                    markup = _markup(*rows)
                else:
                    fmt = parts[3]
                    if fmt not in svc.FORMAT_LABELS:
                        await callback.answer('Неизвестный формат.', show_alert=True)
                        return
                    try:
                        if lesson.status == 'completed':
                            lesson = await svc.confirm_past_format(s, viewer, lesson_id, fmt)
                        elif lesson.status == 'scheduled':
                            lesson = await svc.confirm_format(s, viewer, lesson_id, fmt)
                        else:
                            raise svc.RuleError('Формат отменённого занятия не исправляется.')
                        await s.commit()
                    except svc.RuleError as exc:
                        await callback.answer(str(exc), show_alert=True)
                        return
                    await callback.message.answer('Формат подтверждён.')
                    data = await dashboard.overview(s, settings)
                    lesson = next((x for x in data['lessons'] if x.id == lesson_id), lesson)
                    await _dash_show_lesson(callback, data, lesson, ctx)
                    await callback.answer()
                    return
        await _edit_or_send(callback, text, markup)
        await callback.answer()
        return

    if action in ('subject', 'lesson'):
        if len(parts) < 3 or not parts[2].isdigit() or len(parts[2]) > 10:
            await callback.answer('Раздел не найден.', show_alert=True)
            return
        if action == 'subject':
            subject_id = int(parts[2])
            page_index = 3
            ctx = dashboard.ctx_from_parts(parts, 4, block='subject', page=_int_part(page_index) or 0)
            ctx = replace(ctx, subject_id=subject_id)
            block = 'subject'
        else:
            lesson_id = int(parts[2])
            if len(parts) > 3 and parts[3] in dashboard.CODE_BLOCKS:
                block = dashboard.CODE_BLOCKS[parts[3]]
                page = _int_part(4) or 0
                ctx = dashboard.ctx_from_parts(parts, 5, block=block, page=page)
            else:
                ctx = dashboard.DashContext(block='sessions')
    elif action in paged:
        page = _int_part(2, 0, 6)
        if page is None:
            await callback.answer('Страница не найдена.', show_alert=True)
            return
        ctx = dashboard.ctx_from_parts(parts, 3, block=action, page=page)

    section = None
    lesson = None
    async with sm() as s:
        data = await dashboard.overview(s, settings)
        if action == "csv":
            content = await dashboard.export_csv(s, settings)
            await callback.answer()
            period = 'триместр' if data['term'] else 'вся база (триместр не задан)'
            await callback.message.answer_document(
                BufferedInputFile(content, filename="mentoring_sessions.csv"),
                caption=f"Занятия: {len(data['lessons'])} · {period}. "
                        "Записи, отзывы, проверенные итоги. Разделитель — точка с запятой.")
            return
        if action == "tickets":
            text = ("<b>Обращения</b>\n"
                    f"Открытых в боте: {data['tickets_open']}\n"
                    f"Просрочено по SLA: {data['tickets_overdue']}\n\n"
                    "Очередь, SLA и карточки — в /queue и /report.")
        elif action == 'lesson':
            lesson = next((x for x in data['lessons'] if x.id == lesson_id), None)
            if lesson is None:
                await _dash_bad_context(callback, 'Занятие не найдено в текущем периоде сводки.')
                return
            text = dashboard.lesson_text(data, lesson)
        elif action in paged:
            if action == 'subject' and not any(
                    row['subject_id'] == subject_id for row in data['cycle'].values()):
                await _dash_bad_context(callback, 'Предмет больше недоступен в текущем срезе.')
                return
            section = dashboard.section_page(data, action, page, subject_id, ctx=ctx)
            text = section['text']
            ctx = section['ctx']
        else:
            builders = {"overview": dashboard.overview_text, "kpi": dashboard.kpi_text}
            builder = builders.get(action)
            if builder is None:
                await callback.answer("Раздел не найден.", show_alert=True)
                return
            text = builder(data)
    await _edit_or_send(callback, text, _dash_keyboard(
        section, block=block, subject_id=subject_id, lesson_id=lesson_id,
        lesson=lesson, ctx=ctx))
    await callback.answer()


# ---------- итоговый отчёт ментора (Прил. 4) ----------

@router.message(Command("finalreport"))
async def cmd_final_report(message: Message, state: FSMContext, viewer: Viewer,
                           settings: Settings, sm: async_sessionmaker) -> None:
    if not viewer.is_staff:
        await message.answer(t(viewer.lang, "staff_only"))
        return
    async with sm() as s:
        if not await repo.is_mentor(s, viewer.id):
            await message.answer("Итоговый отчёт сдаёт ментор из реестра /mentors.")
            return
        term = await svc.active_term(s, settings)
        if term is None:
            await message.answer("Сначала задайте триместр: <code>/term осень26 2026-09-07 12</code>")
            return
        stats = await svc.final_stats(s, viewer.id, term, settings)
        existing = await svc.final_report_for(s, viewer.id, term.id)
        roles = await svc.roles_of(s, viewer.id, term.id)
        deadline = svc.final_report_deadline(term, settings)
    role = roles[0][1] if roles else None
    await state.clear()
    await state.update_data(final_term=term.id, final_role=role)
    await state.set_state(FinalForm.summary)
    late = clock.now() > deadline
    card = (f"<b>Итоговый отчёт · {e(term.name, 40)}</b>\n\n"
            f"Слотов проведено: <b>{stats['slots']}</b> · отменено {stats['cancelled']}\n"
            f"Материалов в Банк: <b>{stats['materials']}</b>\n"
            f"Участников всего: <b>{stats['audience_total']}</b>\n"
            f"Роль: {svc.ROLE_LABELS.get(role or '', '—')}\n"
            f"Дедлайн: {deadline:%d.%m.%Y} " + ("<b>просрочен</b>" if late else "— ещё есть время"))
    if existing:
        card += "\n\n⚠️ Отчёт уже сдан. Новая отправка заменит его."
    await message.answer(card + "\n\nШаг 1 из 3. <b>Краткое резюме работы</b>\n"
                                "4–6 предложений: что изменилось у студентов, что получилось. Минимум 40 символов.\n"
                                "/cancel — прервать.")


@router.message(FinalForm.summary, F.text & ~F.text.startswith("/"))
async def final_summary(message: Message, state: FSMContext) -> None:
    text = ' '.join((message.text or '').split())
    if len(text) < 40:
        await message.answer("Резюме — минимум 40 символов (4–6 предложений).")
        return
    await state.update_data(final_summary=text)
    await state.set_state(FinalForm.difficulties)
    await message.answer("Шаг 2 из 3. <b>Сложности и барьеры</b>\n\n"
                         "Что мешало: организационно или методически. Минимум 10 символов.")


@router.message(FinalForm.difficulties, F.text & ~F.text.startswith("/"))
async def final_difficulties(message: Message, state: FSMContext) -> None:
    text = ' '.join((message.text or '').split())
    if len(text) < 10:
        await message.answer("«Сложности и барьеры» — минимум 10 символов.")
        return
    await state.update_data(final_difficulties=text)
    await state.set_state(FinalForm.proposals)
    await message.answer("Шаг 3 из 3. <b>Предложения по улучшению Школы</b>\n\nМинимум 10 символов.")


@router.message(FinalForm.proposals, F.text & ~F.text.startswith("/"))
async def final_proposals(message: Message, state: FSMContext, viewer: Viewer, bot: Bot,
                          settings: Settings, sm: async_sessionmaker) -> None:
    text = ' '.join((message.text or '').split())
    if len(text) < 10:
        await message.answer("«Предложения» — минимум 10 символов.")
        return
    data = await state.get_data()
    async with sm() as s:
        term = await s.get(Term, data.get('final_term'))
        if term is None:
            await state.clear()
            await message.answer("Триместр не найден. Откройте /finalreport заново.")
            return
        try:
            report = await svc.save_final_report(
                s, viewer, term, settings, role=data.get('final_role'),
                summary=data.get('final_summary', ''), difficulties=data.get('final_difficulties', ''),
                proposals=text)
        except svc.RuleError as exc:
            await message.answer(str(exc))
            return
        await s.commit()
        stats = await svc.final_stats(s, viewer.id, term, settings)
        deadline = svc.final_report_deadline(term, settings)
    await state.clear()
    late = clock.now() > deadline
    for admin_id in settings.staff_ids:
        if admin_id == viewer.id:
            continue
        try:
            await bot.send_message(admin_id,
                                   f"📄 <b>{e(viewer.name, 128)}</b> сдал(а) итоговый отчёт за "
                                   f"{e(term.name, 40)}" + (" (с опозданием)" if late else "") + ".")
        except TelegramAPIError:
            pass
    await message.answer(
        f"✅ <b>Итоговый отчёт сохранён.</b>\n\n"
        f"Слотов: {stats['slots']} · материалов: {stats['materials']} · "
        f"участников: {stats['audience_total']}\n"
        f"Дедлайн был {deadline:%d.%m.%Y}" + (" — отчёт сдан с опозданием." if late else ", сдали вовремя."),
        reply_markup=views.main_menu(viewer.lang, viewer.is_staff))


@router.message(Command("finalreports"))
async def cmd_final_reports(message: Message, viewer: Viewer, settings: Settings,
                            sm: async_sessionmaker) -> None:
    if not viewer.is_admin:
        await message.answer(t(viewer.lang, "admin_only"))
        return
    async with sm() as s:
        term = await svc.active_term(s, settings)
        if term is None:
            await message.answer("Триместр не задан: <code>/term осень26 2026-09-07 12</code>")
            return
        data = await svc.final_reports_overview(s, term, settings)
    lines = [f"<b>Итоговые отчёты · {e(term.name, 40)}</b>",
             f"Дедлайн: {data['deadline']:%d.%m.%Y}",
             f"Сдали: {len(data['submitted'])} · не сдали: {len(data['missing'])}", ""]
    for name, report in data['submitted']:
        mark = "⚠️" if name in data['late'] else "✅"
        lines.append(f"{mark} {e(name, 60)} — слотов {report.slots}, материалов {report.materials}")
    if data['missing']:
        lines.append("\n<b>Не сдали:</b>")
        lines += [f"· {e(name, 60)}" for name in data['missing']]
    await message.answer("\n".join(lines))
