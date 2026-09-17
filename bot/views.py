"""Клавиатуры и текстовые карточки. Только отрисовка — никакой логики и БД."""
from __future__ import annotations

from datetime import datetime
from html import escape

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from . import repo
from .db import CATEGORIES, STATUSES, Note, Ticket, User, utcnow
from .texts import CATEGORY_BUTTONS, CATEGORY_EMOJI, MENU, cat_name, fmt_age, status_name, t

TG_LIMIT = 4096   # лимит Telegram — в UTF-16 юнитах, эмодзи занимают два
CARD_BUDGET = 3900


def u16len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def clip(text: str, limit: int) -> str:
    """Обрезать НЕэкранированный текст до limit UTF-16 юнитов. Готовый HTML этим резать нельзя —
    можно разорвать тег, и Telegram ответит 400."""
    if u16len(text) <= limit:
        return text
    while u16len(text) > limit - 1:
        text = text[: max(1, len(text) - max(1, (u16len(text) - limit) // 2 + 1))]
    return text + "…"


def _fit(head: list[str], tail: list[str], budget: int = CARD_BUDGET) -> str:
    """head входит всегда, из tail берём самые свежие строки, пока влезают (целиком, не режем)."""
    used = u16len("\n".join(head))
    kept: list[str] = []
    for line in reversed(tail):
        used += u16len(line) + 1
        if used > budget:
            break
        kept.append(line)
    return "\n".join(head + kept[::-1])


# ---------- меню ----------

def main_menu(lang: str, is_staff: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=MENU["feedback"][lang])],
        [KeyboardButton(text=MENU["status"][lang]), KeyboardButton(text=MENU["help"][lang])],
    ]
    if is_staff:
        rows.append([KeyboardButton(text=MENU["queue"][lang])])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


# ---------- подача обращения ----------

def categories_kb(lang: str) -> InlineKeyboardMarkup:
    order = ("question", "complaint", "suggestion", "praise")
    rows = [[_btn(CATEGORY_BUTTONS[c][lang], f"cat:{c}") for c in order[i:i + 2]] for i in (0, 2)]
    rows.append([_btn(CATEGORY_BUTTONS["serious"][lang], "cat:serious")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def related_kb(lang: str, category: str, mentors: list[User], has_lead: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for m in mentors[:20]:
        pair.append(_btn(f"🎓 {m.display_name[:24]}", f"rel:m:{m.telegram_id}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    if category == "complaint" and has_lead:
        rows.append([_btn(t(lang, "btn_about_coord"), "rel:coord")])
    rows.append([_btn(t(lang, "btn_skip"), "rel:skip")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def serious_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn(t(lang, "btn_coord_no"), "rel:skip")],
        [_btn(t(lang, "btn_coord_yes"), "rel:coord")],
    ])


def skip_kb(lang: str, data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn(t(lang, "btn_skip"), data)]])


def anon_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        _btn(t(lang, "btn_named"), "anon:0"), _btn(t(lang, "btn_anon"), "anon:1"),
    ]])


def rating_kb(ticket_id: int, token: str = "") -> InlineKeyboardMarkup:
    suffix = f":{token}" if token else ""
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn(f"{n} ⭐", f"rate:{ticket_id}:{n}{suffix}") for n in range(1, 6)]
    ])


# ---------- заявитель: список и карточка ----------

def my_tickets_kb(tickets: list[Ticket], lang: str) -> InlineKeyboardMarkup:
    rows = [
        [_btn(f"{CATEGORY_EMOJI[tk.category]} {tk.code} · {status_name(lang, tk.status)}", f"my:{tk.id}")]
        for tk in tickets
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def submitter_card(ticket: Ticket, notes: list[Note], lang: str) -> list[str]:
    """Карточка для заявителя — списком сообщений. Переписку не обрезаем: аноним не получает
    уведомлений, и эта карточка — единственное место, где он может прочитать ответ целиком."""
    head = [
        f"{CATEGORY_EMOJI[ticket.category]} <b>{ticket.code}</b> · {cat_name(lang, ticket.category, emoji=False)}",
        f"{t(lang, 'card_status')}: <b>{status_name(lang, ticket.status)}</b>",
        "",
        escape(clip(ticket.text, 400)),
        "",
        t(lang, "replies_header"),
    ]
    if not notes:
        head.append(t(lang, "no_replies"))
    messages, current = [], head
    for n in notes[-10:]:
        who = t(lang, "you") if n.author_kind == "submitter" else t(lang, "team")
        line = f"<b>{who}</b>, {n.created_at:%d.%m}: {escape(n.text)}"
        if u16len("\n".join(current + [line])) > CARD_BUDGET:
            messages.append("\n".join(current))
            current = []
        current.append(line)
    messages.append("\n".join(current))
    return messages


def submitter_card_kb(ticket: Ticket, lang: str, secret_token: str = "") -> InlineKeyboardMarkup | None:
    rows = []
    if ticket.status != "closed":
        suffix = f":{secret_token}" if secret_token else ""
        rows.append([_btn(t(lang, "btn_add_comment"), f"cm:{ticket.id}{suffix}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# ---------- команда: очередь ----------

def queue_text(tickets: list[Ticket], total: int, category: str, status: str, lang: str,
               names: dict[int, User], now: datetime | None = None) -> str:
    now = now or utcnow()
    cat_label = t(lang, "filter_all") if category == "all" else cat_name(lang, category)
    if status == "open":
        st_label = t(lang, "filter_open")
    elif status == "all":
        st_label = t(lang, "filter_all")
    else:
        st_label = status_name(lang, status)
    lines = [t(lang, "queue_title", cat=cat_label, status=st_label, total=total), ""]
    if not tickets:
        lines.append(t(lang, "queue_empty"))
    for tk in tickets:
        age = fmt_age(lang, (now - tk.created_at).total_seconds())
        marks = ""
        if tk.priority == "urgent":
            marks += " 🔴"
        if repo.is_overdue(tk, now):
            marks += " ⏰"
        if tk.lead_only:
            marks += " 🔒"
        if tk.flagged:
            marks += " 🚩"
        assignee = names.get(tk.assigned_to).display_name if tk.assigned_to in names else "—"
        lines.append(
            f"{CATEGORY_EMOJI[tk.category]} <b>{tk.code}</b> · {status_name(lang, tk.status)} · "
            f"{age} · 👤 {escape(assignee)}{marks}"
        )
    return "\n".join(lines)


def queue_kb(tickets: list[Ticket], total: int, category: str, status: str, page: int,
             page_size: int, lang: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for tk in tickets:
        pair.append(_btn(f"{CATEGORY_EMOJI[tk.category]} {tk.code}", f"tk:{tk.id}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)

    def mark(active: bool, label: str) -> str:
        return f"• {label}" if active else label

    rows.append(
        [_btn(mark(category == "all", t(lang, "filter_all")), f"q:all:{status}:0")]
        + [_btn(mark(category == c, CATEGORY_EMOJI[c]), f"q:{c}:{status}:0") for c in CATEGORIES]
    )
    rows.append([
        _btn(mark(status == "open", t(lang, "filter_open")), f"q:{category}:open:0"),
        _btn(mark(status == "new", status_name(lang, "new")), f"q:{category}:new:0"),
        _btn(mark(status == "resolved", status_name(lang, "resolved")), f"q:{category}:resolved:0"),
        _btn(mark(status == "all", t(lang, "filter_all")), f"q:{category}:all:0"),
    ])
    nav = []
    if page > 0:
        nav.append(_btn("◀️", f"q:{category}:{status}:{page - 1}"))
    nav.append(_btn(t(lang, "btn_refresh"), f"q:{category}:{status}:{page}"))
    if (page + 1) * page_size < total:
        nav.append(_btn("▶️", f"q:{category}:{status}:{page + 1}"))
    rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- команда: карточка ----------

def staff_card(ticket: Ticket, notes: list[Note], lang: str, names: dict[int, User],
               now: datetime | None = None, header: str = "") -> str:
    now = now or utcnow()
    head = f"{CATEGORY_EMOJI[ticket.category]} <b>{ticket.code}</b> · {cat_name(lang, ticket.category, emoji=False)}"
    if ticket.priority == "urgent":
        head += f" · {t(lang, 'card_urgent')}"
    lines = [header, head] if header else [head]
    if ticket.lead_only:
        lines.append(t(lang, "card_lead_only"))
    status_line = (
        f"{t(lang, 'card_status')}: <b>{status_name(lang, ticket.status)}</b> · "
        f"{t(lang, 'card_age')}: {fmt_age(lang, (now - ticket.created_at).total_seconds())}"
    )
    if repo.is_overdue(ticket, now):
        status_line += f" · {t(lang, 'card_overdue')}"
    lines.append(status_line)
    if ticket.is_anonymous:
        lines.append(f"{t(lang, 'card_from')}: <i>{t(lang, 'card_anonymous')}</i>")
    else:
        lines.append(f"{t(lang, 'card_from')}: {escape(ticket.submitter_name or '?')}")
    if ticket.related_course:
        lines.append(f"{t(lang, 'card_related')}: {escape(ticket.related_course)}")
    if ticket.mentor_id:
        mentor = names.get(ticket.mentor_id)
        lines.append(f"{t(lang, 'card_mentor')}: {escape(mentor.display_name if mentor else str(ticket.mentor_id))}")
    assignee = names.get(ticket.assigned_to) if ticket.assigned_to else None
    lines.append(f"{t(lang, 'card_assigned')}: {escape(assignee.display_name) if assignee else '—'}")
    if ticket.flagged:
        lines.append(f"{t(lang, 'card_flagged')}: {escape(ticket.flagged)}")
    if ticket.attachment_file_id:
        lines.append(t(lang, "card_has_attachment"))
    if ticket.satisfaction_rating:
        lines.append(f"{t(lang, 'card_rating')}: {'⭐' * ticket.satisfaction_rating}")
    lines += ["", escape(ticket.text)]  # текст обращения команда видит всегда целиком
    tail: list[str] = []
    for n in notes:
        if n.author_kind == "submitter":
            tag, who = "✏️", t(lang, "note_submitter")
        elif n.visible_to_submitter:
            tag, who = "💬", f"{escape(n.author_name)} ({t(lang, 'note_reply')})"
        else:
            tag, who = "📝", f"{escape(n.author_name)} ({t(lang, 'note_internal')})"
        tail.append(f"{tag} <b>{who}</b>, {n.created_at:%d.%m %H:%M}: {escape(clip(n.text, 500))}")
    if tail:
        lines += ["", t(lang, "card_notes")]
    return _fit(lines, tail)


def staff_card_kb(ticket: Ticket, lang: str, can_assign: bool) -> InlineKeyboardMarkup:
    others = [s for s in STATUSES if s != ticket.status and s != "new"]
    rows = [[_btn(status_name(lang, s), f"st:{ticket.id}:{s}") for s in others[i:i + 2]]
            for i in range(0, len(others), 2)]
    rows.append([_btn(t(lang, "btn_reply"), f"rp:{ticket.id}"), _btn(t(lang, "btn_note"), f"nt:{ticket.id}")])
    last = []
    if can_assign:
        last.append(_btn(t(lang, "btn_assign"), f"as:{ticket.id}"))
    if ticket.attachment_file_id:
        last.append(_btn(t(lang, "btn_attachment"), f"at:{ticket.id}"))
    if last:
        rows.append(last)
    rows.append([_btn(t(lang, "btn_back_queue"), "q:all:open:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def open_ticket_kb(ticket: Ticket, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn(t(lang, "btn_open", code=ticket.code), f"tk:{ticket.id}")]])


def assign_kb(ticket: Ticket, candidates: list[User], lang: str) -> InlineKeyboardMarkup:
    rows = [[_btn(f"👤 {u.display_name[:30]}", f"asg:{ticket.id}:{u.telegram_id}")] for u in candidates[:30]]
    if ticket.assigned_to:
        rows.append([_btn(t(lang, "btn_unassign"), f"asg:{ticket.id}:0")])
    rows.append([_btn(t(lang, "btn_back"), f"tk:{ticket.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
