"""Модели и подключение к БД (раздел 9 спека).

Важно для анонимности: у анонимного тикета submitter_id и submitter_name = NULL,
время создания округляется до часа, а у пользователей нет никаких отметок времени —
сопоставить «кто был активен в эту минуту» по базе нельзя.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer,
                        JSON, String, Table, Text, UniqueConstraint, select, text)
from sqlalchemy.dialects.postgresql import Insert as PGInsert, insert as pg_insert
from sqlalchemy.dialects.sqlite import Insert as SQLiteInsert, insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

CATEGORIES = ("question", "complaint", "suggestion", "praise", "serious")
STATUSES = ("new", "in_review", "in_progress", "resolved", "closed")
OPEN_STATUSES = ("new", "in_review", "in_progress")

# Функциональные роли Hard-ментора (п. 5.9). Роль привязана к предмету.
MENTOR_ROLES = ("lead", "respondent", "author")

# Единый реестр стаффа: статус участия и принадлежность к контуру.
STAFF_STATUSES = ("active", "paused", "left")
STAFF_TEAMS = ("hard", "hard_leadership", "school_leadership")

# Три зоны ответственности, согласованные владельцем для первой версии.
RESPONSIBILITY_ZONES = ("subject", "schedule", "materials")

# Номенклатура форматов занятий Hard-ментора (п. 8.1.2).
SESSION_FORMATS = (
    "syllabus_clinic",   # аудит курса, 1–2-я недели
    "how_to_start",      # барьер старта лабы
    "weekly_errors",     # разбор ошибок недели
    "emergency_hours",   # экспресс 15–20 мин накануне дедлайна
    "intensive",         # групповой интенсив, пиковые недели 5/9/10
    "one_on_one",        # консультация по входному билету
    "async_answers",     # ответы в чатах
    "mock_review",       # разбор пробного экзамена
    "cheat_sheet",       # памятка / калькулятор оценок
)


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
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.telegram_id"), nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"))
    starts_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    location: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(16), default="scheduled", index=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(400), nullable=True)
    checklist_items: Mapped[list] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    # Триместр: нужен для номера недели и исключения недель 5/9/10 (п. 8.1.3).
    term_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # Формат занятия по п. 8.1.2 — по нему считается охват Syllabus Clinic (п. 11.3).
    format: Mapped[str] = mapped_column(String(24), default="one_on_one",
                                        server_default="one_on_one")
    format_confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


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
    # Входной билет по п. 8.1.4 / Прил. 5: что студент уже пробовал и самооценка 1–5.
    tried: Mapped[str] = mapped_column(Text, default="", server_default="")
    self_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)


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
    # document — PDF/DOCX; photo — снимок доски/заметок (Telegram photo).
    media_type: Mapped[str] = mapped_column(String(8), default="document", server_default="document")
    kind: Mapped[str] = mapped_column(String(12))
    visibility: Mapped[str] = mapped_column(String(10), default="students")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    cycle_stage: Mapped[str | None] = mapped_column(String(16), nullable=True)
    review_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    lecturer_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_recorded_by: Mapped[int | None] = mapped_column(ForeignKey("users.telegram_id"), nullable=True)


class MentorWeeklyReport(Base):
    __tablename__ = "mentor_weekly_reports"
    __table_args__ = (
        UniqueConstraint("mentor_id", "week_start", name="uq_mentor_weekly_report"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mentor_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), index=True)
    # ISO Monday date (YYYY-MM-DD). Kept as text for portable SQLite/Postgres migrations.
    week_start: Mapped[str] = mapped_column(String(10), index=True)
    # Собранный текст отчёта — для списков и уведомлений.
    content: Mapped[str] = mapped_column(Text)
    # Поля п. 9.3, которые ментор пишет руками; цифры бот считает сам.
    insights: Mapped[str] = mapped_column(Text, default="", server_default="")
    focus: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FinalReport(Base):
    """Итоговый отчёт ментора — Приложение 4, за 3 рабочих дня до конца триместра.

    Цифры (слоты, материалы, охват) бот считает сам; ментор пишет смысл.
    """
    __tablename__ = "mentor_final_reports"
    __table_args__ = (UniqueConstraint("mentor_id", "term_id", name="uq_final_report"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mentor_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), index=True)
    term_id: Mapped[int] = mapped_column(ForeignKey("terms.id"), index=True)
    direction: Mapped[str] = mapped_column(String(8), default="hard")
    role: Mapped[str | None] = mapped_column(String(12), nullable=True)
    slots: Mapped[int] = mapped_column(Integer, default=0)
    events: Mapped[int] = mapped_column(Integer, default=0)
    collabs: Mapped[int] = mapped_column(Integer, default=0)
    materials: Mapped[int] = mapped_column(Integer, default=0)
    checkpoints: Mapped[int] = mapped_column(Integer, default=0)
    audience_total: Mapped[int] = mapped_column(Integer, default=0)
    audience_first: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    difficulties: Mapped[str] = mapped_column(Text, default="")
    proposals: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


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


class Term(Base):
    """Триместр: понедельник старта и длина в неделях (п. 8.1.5).

    Даёт номер недели, исключение пиковых недель 5/9/10 из правила
    «меньше двух записей → отмена» (п. 8.1.3) и дедлайн итогового отчёта.
    """
    __tablename__ = "terms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    # ISO-дата понедельника первой недели; текст для переносимости SQLite/Postgres.
    starts_on: Mapped[str] = mapped_column(String(10))
    weeks: Mapped[int] = mapped_column(Integer, default=12)
    # Размер потока первокурсников — знаменатель для охвата Syllabus Clinic (п. 11.3).
    cohort: Mapped[int] = mapped_column(Integer, default=2500, server_default="2500")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class MentorRole(Base):
    """Роль ментора на предмет и триместр (п. 5.9).

    Один человек может быть Ведущим по одному предмету и Респондентом
    по другому. Смена роли — только через Главного ментора и Зама.
    """
    __tablename__ = "mentor_roles"
    __table_args__ = (
        UniqueConstraint("telegram_id", "subject_id", "term_id", name="uq_mentor_role"),
        CheckConstraint("role IN ('lead','respondent','author')", name="ck_mentor_role"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("mentor_subjects.id"), index=True)
    term_id: Mapped[int] = mapped_column(ForeignKey("terms.id"), index=True)
    role: Mapped[str] = mapped_column(String(12))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class StaffMember(Base):
    """Профиль человека в едином реестре стаффа.

    Не заменяет `mentors` и `mentor_roles`: членство в менторах и роли на
    предмет остаются источники истины для доступа. Здесь только то, чего в
    этих таблицах нет: статус участия, контур команды и должность.
    """
    __tablename__ = "staff_members"
    __table_args__ = (
        CheckConstraint("status IS NULL OR status IN ('active','paused','left')", name="ck_staff_status"),
        CheckConstraint("team IS NULL OR team IN ('hard','hard_leadership','school_leadership')",
                        name="ck_staff_team"),
    )

    telegram_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id", ondelete="CASCADE"), primary_key=True, autoincrement=False)
    status: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    team: Mapped[str | None] = mapped_column(String(24), nullable=True)
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.telegram_id"), nullable=True)


class ResponsibilityAssignment(Base):
    """История ответственных по зонам: предмет, расписание, материалы.

    Активное назначение — `ended_at IS NULL`. Завершённые не удаляются:
    при смене человека сохраняется, кто и за что отвечал и когда.
    `subject_id` обязателен только для зоны `subject`; расписание и материалы
    в первой версии общие для всей команды.
    """
    __tablename__ = "responsibility_assignments"
    __table_args__ = (
        CheckConstraint("zone IN ('subject','schedule','materials')", name="ck_resp_zone"),
        CheckConstraint(
            "(zone = 'subject' AND subject_id IS NOT NULL) OR "
            "(zone <> 'subject' AND subject_id IS NULL)",
            name="ck_resp_subject"),
        CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="ck_resp_dates"),
        Index("uq_resp_subject_active", "subject_id", unique=True,
              sqlite_where=text("ended_at IS NULL AND zone = 'subject'"),
              postgresql_where=text("ended_at IS NULL AND zone = 'subject'")),
        Index("uq_resp_global_active", "zone", unique=True,
              sqlite_where=text("ended_at IS NULL AND zone IN ('schedule','materials')"),
              postgresql_where=text("ended_at IS NULL AND zone IN ('schedule','materials')")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), index=True)
    zone: Mapped[str] = mapped_column(String(16), index=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("mentor_subjects.id"), nullable=True, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"))
    ended_by: Mapped[int | None] = mapped_column(ForeignKey("users.telegram_id"), nullable=True)


class StaffHistory(Base):
    """Историческая запись о человеке без учётной записи в боте.

    Нужна для вышедших людей, которых сохраняют в реестре, но у которых нет
    `users.telegram_id`. Это не аккаунт и не назначение: только имя, username,
    контур, должность, статус и источник. Telegram ID не выдумывается.
    """
    __tablename__ = "staff_history"
    __table_args__ = (
        CheckConstraint("status IS NULL OR status IN ('active','paused','left')",
                        name="ck_staff_history_status"),
        CheckConstraint("team IS NULL OR team IN ('hard','hard_leadership','school_leadership')",
                        name="ck_staff_history_team"),
        UniqueConstraint("username", name="uq_staff_history_username"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    team: Mapped[str | None] = mapped_column(String(24), nullable=True)
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.telegram_id"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.telegram_id"), nullable=True)


class SessionFormatChange(Base):
    """История подтверждения формата занятия: кто, когда и что изменил.

    Нужна, чтобы для старых занятий было видно, что формат не «сам появился»,
    а был явно указан администратором. Старое значение сохраняется как было,
    включая `one_on_one` при `format_confirmed=NULL`.
    """
    __tablename__ = "session_format_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("mentoring_sessions.id"), index=True)
    old_format: Mapped[str | None] = mapped_column(String(24), nullable=True)
    old_confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    new_format: Mapped[str] = mapped_column(String(24))
    changed_by: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), index=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SessionFeedback(Base):
    """Обратная связь студента после занятия — Приложение 11.

    Оси KPI (п. 11.2) выводятся из ответов, а не спрашиваются отдельно:
    доступность ← clarity, компетентность ← overall, этика ← honesty.
    """
    __tablename__ = "session_feedback"
    __table_args__ = (
        UniqueConstraint("session_id", "student_id", name="uq_session_feedback"),
        CheckConstraint("progressed IN ('yes','partial','no')", name="ck_feedback_progressed"),
        CheckConstraint("honesty IN ('ok','violation')", name="ck_feedback_honesty"),
        CheckConstraint("clarity BETWEEN 1 AND 5", name="ck_feedback_clarity"),
        CheckConstraint("overall BETWEEN 1 AND 5", name="ck_feedback_overall"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("mentoring_sessions.id"), index=True)
    mentor_id: Mapped[int] = mapped_column(BigInteger, index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), index=True)
    clarity: Mapped[int] = mapped_column(Integer)
    progressed: Mapped[str] = mapped_column(String(8))
    honesty: Mapped[str] = mapped_column(String(9))
    best: Mapped[str] = mapped_column(Text, default="")
    next_topics: Mapped[str] = mapped_column(Text, default="")
    overall: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    @property
    def availability(self) -> int:
        return self.clarity

    @property
    def competence(self) -> int:
        return self.overall

    @property
    def ethics(self) -> int:
        return 5 if self.honesty == "ok" else 1

    @property
    def score(self) -> float:
        return round((self.availability + self.competence + self.ethics) / 3, 2)


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
