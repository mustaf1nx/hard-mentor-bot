"""Модели и подключение к БД (раздел 9 спека).

Важно для анонимности: у анонимного тикета submitter_id и submitter_name = NULL,
время создания округляется до часа, а у пользователей нет никаких отметок времени —
сопоставить «кто был активен в эту минуту» по базе нельзя.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Integer,
                        JSON, String, Table, Text, UniqueConstraint, select, text)
from sqlalchemy.dialects.postgresql import Insert as PGInsert, insert as pg_insert
from sqlalchemy.dialects.sqlite import Insert as SQLiteInsert, insert as sqlite_insert
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
    # Legacy column: retained for existing databases, NOT an authorization source.
    # Only the one-time migration below reads it. New memberships live in mentors.
    role: Mapped[str] = mapped_column(String(16), default="mentee")
    lang: Mapped[str] = mapped_column(String(2), default="ru")
    cohort: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Mentor(Base):
    __tablename__ = "mentors"

    # No timestamps: membership must not become a record of user activity.
    telegram_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"),
        primary_key=True, autoincrement=False,
    )


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


class TicketNotification(Base):
    """Durable deliveries of new tickets, committed together with the ticket.

    Only staff recipient IDs are stored, never the submitter or message body.
    Transient retry/lease times are cleared after sent/failed/skipped outcomes;
    no permanent created_at/sent_at timestamps are added for anonymous tickets.
    Recipient 0 is a routing placeholder and is never used as a Telegram chat ID.
    """
    __tablename__ = "ticket_notifications"
    __table_args__ = (
        UniqueConstraint("ticket_id", "recipient_id", name="uq_ticket_notification_recipient"),
        CheckConstraint("status IN ('pending','sending','sent','failed','skipped')",
                        name="ck_ticket_notification_status"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    recipient_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # An error class/code only, NEVER the raw Telegram error or message contents.
    last_error: Mapped[str | None] = mapped_column(String(64), nullable=True)


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



# Mentoring sessions are an explicitly identified workflow, independent of anonymous tickets.
class Subject(Base):
    __tablename__ = "mentor_subjects"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    name_key: Mapped[str] = mapped_column(String(300), unique=True)


class Topic(Base):
    __tablename__ = "mentor_topics"
    __table_args__ = (UniqueConstraint("subject_id", "name_key", name="uq_topic_name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("mentor_subjects.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    name_key: Mapped[str] = mapped_column(String(360))


class TopicChecklist(Base):
    __tablename__ = "topic_checklists"
    topic_id: Mapped[int] = mapped_column(ForeignKey("mentor_topics.id"), primary_key=True)
    items: Mapped[list] = mapped_column(JSON)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"))


class MentoringSession(Base):
    __tablename__ = "mentoring_sessions"
    __table_args__ = (
        CheckConstraint("status IN ('scheduled','completed','cancelled')", name="ck_session_status"),
        CheckConstraint("ends_at > starts_at", name="ck_session_interval"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    creation_key: Mapped[str] = mapped_column(String(40), unique=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("mentor_topics.id"), index=True)
    # FK points at users, not mentors: removing membership must not destroy history.
    mentor_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"))
    starts_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    location: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(16), default="scheduled", index=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(400), nullable=True)
    checklist_items: Mapped[list] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class SessionBooking(Base):
    __tablename__ = "session_bookings"
    __table_args__ = (
        UniqueConstraint("session_id", "student_id", name="uq_session_student"),
        CheckConstraint("status IN ('active','withdrawn')", name="ck_booking_status"),
        CheckConstraint("attendance IN ('unknown','present','absent')", name="ck_attendance"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("mentoring_sessions.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), index=True)
    question: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    attendance: Mapped[str] = mapped_column(String(10), default="unknown")


class LearningMaterial(Base):
    __tablename__ = "learning_materials"
    __table_args__ = (
        CheckConstraint("kind IN ('recap','material')", name="ck_material_kind"),
        CheckConstraint("visibility IN ('students','staff')", name="ck_material_visibility"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("mentor_topics.id"), index=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("mentoring_sessions.id"), nullable=True, index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), index=True)
    file_id: Mapped[str] = mapped_column(String(512))
    file_unique_id: Mapped[str] = mapped_column(String(256))
    file_name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(12))
    visibility: Mapped[str] = mapped_column(String(10), default="students")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)


class MentoringNotification(Base):
    __tablename__ = "mentoring_notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_key: Mapped[str] = mapped_column(String(160), unique=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("mentoring_sessions.id"), index=True)
    booking_id: Mapped[int | None] = mapped_column(ForeignKey("session_bookings.id"), nullable=True)
    recipient_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(24))
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # A lease permits recovery if the process exits between claiming and delivering.
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


def make_engine(url: str, connect_args: dict | None = None) -> AsyncEngine:
    kwargs: dict = {"connect_args": connect_args or {}}
    if not url.startswith("sqlite"):
        kwargs["pool_pre_ping"] = True  # Railway может рвать простаивающие соединения
    return create_async_engine(url, **kwargs)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


MENTORS_MIGRATION_KEY = "schema:mentors:v1"


def conflict_insert(table: Table, dialect: str) -> PGInsert | SQLiteInsert:
    """An explicit dialect insert for idempotent membership changes."""
    if dialect == "postgresql":
        return pg_insert(table)
    if dialect == "sqlite":
        return sqlite_insert(table)
    raise ValueError(f"Unsupported database dialect: {dialect}")


async def init_db(engine: AsyncEngine) -> None:
    """Create missing tables and migrate legacy mentors exactly once, atomically.

    The marker is essential: DELETE FROM mentors must survive later restarts even
    when users.role still contains the old value. Never drop existing tables/data.
    """
    async with engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect == "postgresql":
            # Serialize schema initialization if two deployments overlap briefly.
            await conn.execute(text("SELECT pg_advisory_xact_lock(72610540931001)"))
        await conn.run_sync(Base.metadata.create_all)
        migrated = await conn.scalar(select(Meta.value).where(Meta.key == MENTORS_MIGRATION_KEY))
        if migrated is not None:
            return
        await conn.execute(
            conflict_insert(Mentor.__table__, dialect)
            .from_select(["telegram_id"], select(User.telegram_id).where(User.role == "mentor"))
            .on_conflict_do_nothing(index_elements=["telegram_id"])
        )
        await conn.execute(
            conflict_insert(Meta.__table__, dialect)
            .values(key=MENTORS_MIGRATION_KEY, value="done")
            .on_conflict_do_nothing(index_elements=["key"])
        )
