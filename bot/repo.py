"""Все запросы к БД в одном месте. Хэндлеры сюда ходят, напрямую SQL не пишут."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import SLA_HOURS
from .db import OPEN_STATUSES, Meta, Note, Ticket, User, utcnow

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # без похожих O/0, I/1, L


@dataclass(frozen=True)
class Viewer:
    """Кто сейчас пишет боту — с уже вычисленной ролью."""

    id: int
    role: str  # mentee / mentor / coordinator / lead
    lang: str
    name: str
    username: str | None = None

    @property
    def is_staff(self) -> bool:
        return self.role in ("mentor", "coordinator", "lead")

    @property
    def is_admin(self) -> bool:
        return self.role in ("coordinator", "lead")


# ---------- пользователи ----------

async def upsert_user(
    session: AsyncSession, telegram_id: int, display_name: str, username: str | None, default_lang: str
) -> User:
    user = await session.get(User, telegram_id)
    username = username.lower() if username else None
    if user is None:
        user = User(
            telegram_id=telegram_id,
            display_name=display_name[:128],
            username=username,
            lang=default_lang,
        )
        session.add(user)
        await session.commit()
    elif user.display_name != display_name[:128] or user.username != username:
        user.display_name = display_name[:128]
        user.username = username
        await session.commit()
    return user


async def get_user(session: AsyncSession, telegram_id: int) -> User | None:
    return await session.get(User, telegram_id)


async def find_user(session: AsyncSession, ref: str) -> User | None:
    """Поиск по числовому ID или @username (пользователь должен был нажать /start)."""
    ref = ref.strip()
    if ref.lstrip("-").isdigit():
        return await session.get(User, int(ref))
    res = await session.execute(select(User).where(User.username == ref.lstrip("@").lower()))
    return res.scalars().first()


async def set_lang(session: AsyncSession, telegram_id: int, lang: str) -> None:
    user = await session.get(User, telegram_id)
    if user:
        user.lang = lang
        await session.commit()


async def set_role(session: AsyncSession, telegram_id: int, role: str) -> None:
    user = await session.get(User, telegram_id)
    if user:
        user.role = role
        await session.commit()


async def list_mentors(session: AsyncSession) -> list[User]:
    res = await session.execute(select(User).where(User.role == "mentor").order_by(User.display_name))
    return list(res.scalars())


async def users_by_ids(session: AsyncSession, ids) -> dict[int, User]:
    ids = list(ids)
    if not ids:
        return {}
    res = await session.execute(select(User).where(User.telegram_id.in_(ids)))
    return {u.telegram_id: u for u in res.scalars()}


# ---------- код доступа к анонимному тикету ----------

def new_access_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def hash_access_code(secret: bytes, code: str) -> str:
    norm = code.replace("-", "").replace(" ", "").upper()
    return hmac.new(secret, norm.encode(), hashlib.sha256).hexdigest()


def check_access_code(secret: bytes, ticket: Ticket, code: str) -> bool:
    if not ticket.access_hash:
        return False
    return hmac.compare_digest(ticket.access_hash, hash_access_code(secret, code))


def ticket_token(secret: bytes, ticket_id: int) -> str:
    """Подпись для кнопок под анонимным тикетом (оценка, дополнение).

    Кнопки с ней бот показывает только тому, кто предъявил код доступа, а подделать
    callback_data без секрета нельзя."""
    return hmac.new(secret, f"ticket:{ticket_id}".encode(), hashlib.sha256).hexdigest()[:10]


# ---------- тикеты ----------

async def create_ticket(
    session: AsyncSession,
    *,
    category: str,
    text: str,
    is_anonymous: bool,
    submitter_id: int | None,
    submitter_name: str | None,
    related_course: str | None = None,
    mentor_id: int | None = None,
    attachment_file_id: str | None = None,
    attachment_type: str | None = None,
    lead_only: bool = False,
    flagged: str | None = None,
    access_hash: str | None = None,
    status: str = "new",
    assigned_to: int | None = None,
    now: datetime | None = None,
) -> Ticket:
    now = now or utcnow()
    if is_anonymous:
        # Гарантия анонимности живёт здесь, а не в хэндлере: что бы ни передали,
        # личность в строку тикета не попадёт. Время — с точностью до часа.
        submitter_id = None
        submitter_name = None
        now = now.replace(minute=0, second=0, microsecond=0)
    else:
        access_hash = None
    ticket = Ticket(
        category=category,
        text=text,
        related_course=(related_course or None) and related_course[:200],
        mentor_id=mentor_id,
        attachment_file_id=attachment_file_id,
        attachment_type=attachment_type,
        is_anonymous=is_anonymous,
        submitter_id=submitter_id,
        submitter_name=submitter_name,
        access_hash=access_hash,
        status=status,
        assigned_to=assigned_to,
        priority="urgent" if category == "serious" else "normal",
        lead_only=lead_only,
        flagged=flagged,
        created_at=now,
        updated_at=now,
    )
    session.add(ticket)
    await session.commit()
    return ticket


async def get_ticket(session: AsyncSession, ticket_id: int) -> Ticket | None:
    return await session.get(Ticket, ticket_id)


def can_view(viewer: Viewer, ticket: Ticket) -> bool:
    if viewer.role == "lead":
        return True
    if ticket.lead_only:
        return False  # жалобы на координатора координатор не видит никогда
    if viewer.role == "coordinator":
        return True
    if viewer.role == "mentor":
        return ticket.assigned_to == viewer.id
    return False


def _visibility_clause(viewer: Viewer):
    if viewer.role == "lead":
        return None
    if viewer.role == "coordinator":
        return Ticket.lead_only.is_(False)
    return (Ticket.assigned_to == viewer.id) & Ticket.lead_only.is_(False)


async def list_tickets(
    session: AsyncSession, viewer: Viewer, category: str = "all", status: str = "open",
    page: int = 0, page_size: int = 8,
) -> tuple[list[Ticket], int]:
    conds = []
    vis = _visibility_clause(viewer)
    if vis is not None:
        conds.append(vis)
    if category != "all":
        conds.append(Ticket.category == category)
    if status == "open":
        conds.append(Ticket.status.in_(OPEN_STATUSES))
    elif status != "all":
        conds.append(Ticket.status == status)
    total = (await session.execute(select(func.count(Ticket.id)).where(*conds))).scalar_one()
    res = await session.execute(
        select(Ticket).where(*conds)
        .order_by(Ticket.priority.desc(), Ticket.created_at.asc(), Ticket.id.asc())
        .offset(page * page_size).limit(page_size)
    )
    return list(res.scalars()), total


async def list_user_tickets(session: AsyncSession, submitter_id: int, limit: int = 10) -> list[Ticket]:
    res = await session.execute(
        select(Ticket).where(Ticket.submitter_id == submitter_id)
        .order_by(Ticket.id.desc()).limit(limit)
    )
    return list(res.scalars())


def mark_first_response(ticket: Ticket, now: datetime | None = None) -> None:
    if ticket.first_response_at is None:
        ticket.first_response_at = now or utcnow()


async def set_status(session: AsyncSession, ticket: Ticket, status: str, now: datetime | None = None) -> None:
    now = now or utcnow()
    ticket.status = status
    ticket.updated_at = now
    mark_first_response(ticket, now)
    if status == "resolved":
        ticket.resolved_at = now
    await session.commit()


async def assign(session: AsyncSession, ticket: Ticket, user_id: int | None) -> None:
    ticket.assigned_to = user_id
    ticket.updated_at = utcnow()
    await session.commit()


async def set_rating(session: AsyncSession, ticket: Ticket, rating: int) -> None:
    ticket.satisfaction_rating = rating
    await session.commit()


async def add_note(
    session: AsyncSession, ticket: Ticket, *, text: str, visible: bool,
    author_kind: str = "staff", author_id: int | None = None, author_name: str = "",
) -> Note:
    now = utcnow()
    if author_kind == "submitter":
        author_id = None  # заявителя в заметках не идентифицируем никогда
        author_name = ""
    note = Note(
        ticket_id=ticket.id, author_kind=author_kind, author_id=author_id,
        author_name=author_name[:160], text=text, visible_to_submitter=visible, created_at=now,
    )
    session.add(note)
    ticket.updated_at = now
    if author_kind == "staff" and visible:
        mark_first_response(ticket, now)
    await session.commit()
    return note


async def list_notes(session: AsyncSession, ticket_id: int, visible_only: bool = False) -> list[Note]:
    q = select(Note).where(Note.ticket_id == ticket_id)
    if visible_only:
        q = q.where(Note.visible_to_submitter.is_(True))
    res = await session.execute(q.order_by(Note.id.asc()))
    return list(res.scalars())


# ---------- SLA и отчёты ----------

def sla_deadline(ticket: Ticket) -> datetime | None:
    hours = SLA_HOURS.get(ticket.category)
    return None if hours is None else ticket.created_at + timedelta(hours=hours)


def is_overdue(ticket: Ticket, now: datetime | None = None) -> bool:
    deadline = sla_deadline(ticket)
    if deadline is None or ticket.first_response_at is not None:
        return False
    if ticket.status not in OPEN_STATUSES:
        return False
    return (now or utcnow()) > deadline


async def overdue_unalerted(session: AsyncSession, now: datetime | None = None) -> list[Ticket]:
    now = now or utcnow()
    res = await session.execute(
        select(Ticket).where(
            Ticket.first_response_at.is_(None),
            Ticket.status.in_(OPEN_STATUSES),
            Ticket.sla_alerted.is_(False),
        )
    )
    return [t for t in res.scalars() if is_overdue(t, now)]


async def tickets_between(session: AsyncSession, start: datetime, end: datetime) -> list[Ticket]:
    res = await session.execute(
        select(Ticket).where(Ticket.created_at >= start, Ticket.created_at < end)
    )
    return list(res.scalars())


async def open_tickets(session: AsyncSession) -> list[Ticket]:
    res = await session.execute(select(Ticket).where(Ticket.status.in_(OPEN_STATUSES)))
    return list(res.scalars())


# ---------- служебное ----------

async def meta_get(session: AsyncSession, key: str) -> str | None:
    row = await session.get(Meta, key)
    return row.value if row else None


async def meta_set(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(Meta, key)
    if row:
        row.value = value
    else:
        session.add(Meta(key=key, value=value))
    await session.commit()
