"""Слайс 1: триместр, роль на предмет, поля входного билета.

Всё идемпотентно: на пустой базе схема уже создана через `create_all`
(так работают тесты и `init_db`), поэтому каждая операция проверяет,
нет ли уже таблицы или колонки.

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    tables = _tables()

    if "terms" not in tables:
        op.create_table(
            "terms",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(60), nullable=False, unique=True),
            sa.Column("starts_on", sa.String(10), nullable=False),
            sa.Column("weeks", sa.Integer(), nullable=False, server_default="11"),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        )
        op.create_index("ix_terms_active", "terms", ["active"])

    if "mentor_roles" not in tables:
        op.create_table(
            "mentor_roles",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("telegram_id", sa.BigInteger(),
                      sa.ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False),
            sa.Column("subject_id", sa.Integer(), sa.ForeignKey("mentor_subjects.id"), nullable=False),
            sa.Column("term_id", sa.Integer(), sa.ForeignKey("terms.id"), nullable=False),
            sa.Column("role", sa.String(12), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("telegram_id", "subject_id", "term_id", name="uq_mentor_role"),
            sa.CheckConstraint("role IN ('lead','respondent','author')", name="ck_mentor_role"),
        )
        op.create_index("ix_mentor_roles_telegram_id", "mentor_roles", ["telegram_id"])
        op.create_index("ix_mentor_roles_subject_id", "mentor_roles", ["subject_id"])
        op.create_index("ix_mentor_roles_term_id", "mentor_roles", ["term_id"])

    if "session_bookings" in _tables() and "tried" not in _columns("session_bookings"):
        op.add_column("session_bookings", sa.Column("tried", sa.Text(), nullable=False, server_default=""))
    if "session_bookings" in _tables() and "self_rating" not in _columns("session_bookings"):
        op.add_column("session_bookings", sa.Column("self_rating", sa.Integer(), nullable=True))

    if "mentoring_sessions" in _tables() and "term_id" not in _columns("mentoring_sessions"):
        op.add_column("mentoring_sessions", sa.Column("term_id", sa.Integer(), nullable=True))
        op.create_index("ix_mentoring_sessions_term_id", "mentoring_sessions", ["term_id"])


def downgrade() -> None:
    tables = _tables()
    if "mentoring_sessions" in tables and "term_id" in _columns("mentoring_sessions"):
        op.drop_index("ix_mentoring_sessions_term_id", table_name="mentoring_sessions")
        op.drop_column("mentoring_sessions", "term_id")
    if "session_bookings" in tables:
        cols = _columns("session_bookings")
        if "self_rating" in cols:
            op.drop_column("session_bookings", "self_rating")
        if "tried" in cols:
            op.drop_column("session_bookings", "tried")
    if "mentor_roles" in tables:
        op.drop_table("mentor_roles")
    if "terms" in tables:
        op.drop_table("terms")
