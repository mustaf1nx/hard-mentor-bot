"""Модели и подключение к БД (раздел 9 спека).

Важно для анонимности: у анонимного тикета submitter_id и submitter_name = NULL,
время создания округляется до часа, а у пользователей нет никаких отметок времени —
сопоставить «кто был активен в эту минуту» по базе нельзя.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

CATEGORIES = ("question", "complaint", "suggestion", "praise", "serious")
STATUSES = ("new", "in_review", "in_progress", "resolved", "closed")
OPEN_STATUSES = ("new", "in_review", "in_progress")


def utcnow() -> datetime:
    """Наивное UTC-время: одинаково ведёт себя в Postgres и SQLite."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ticket_code(ticket_id: int) -> str:
    return f"HM-{ticket_id:04d}"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(16), default="mentee")  # mentee / mentor
    lang: Mapped[str] = mapped_column(String(2), default="ru")
    cohort: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(16), index=True)
    related_course: Mapped[str | None] = mapped_column(String(200), nullable=True)
    mentor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    attachment_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    attachment_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=False)
    submitter_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    submitter_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    access_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    priority: Mapped[str] = mapped_column(String(8), default="normal")  # normal / urgent
    lead_only: Mapped[bool] = mapped_column(Boolean, default=False)
    flagged: Mapped[str | None] = mapped_column(String(120), nullable=True)
    assigned_to: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    satisfaction_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sla_alerted: Mapped[bool] = mapped_column(Boolean, default=False)

    @property
    def code(self) -> str:
        return ticket_code(self.id)


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    # author_kind: staff / submitter. Для заявителя author_id всегда NULL.
    author_kind: Mapped[str] = mapped_column(String(12), default="staff")
    author_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    author_name: Mapped[str] = mapped_column(String(160), default="")
    text: Mapped[str] = mapped_column(Text)
    visible_to_submitter: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Meta(Base):
    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(256), default="")


def make_engine(url: str, connect_args: dict | None = None) -> AsyncEngine:
    kwargs: dict = {"connect_args": connect_args or {}}
    if not url.startswith("sqlite"):
        kwargs["pool_pre_ping"] = True  # Railway может рвать простаивающие соединения
    return create_async_engine(url, **kwargs)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
