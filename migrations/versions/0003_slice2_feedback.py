"""Слайс 2: таблица обратной связи после занятия (Прил. 11).

Идемпотентна: на базе, созданной через create_all, таблица уже есть.

Revision ID: 0003
Revises: 0002
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "session_feedback" in _tables():
        return
    op.create_table(
        "session_feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("mentoring_sessions.id"), nullable=False),
        sa.Column("mentor_id", sa.BigInteger(), nullable=False),
        sa.Column("student_id", sa.BigInteger(), sa.ForeignKey("users.telegram_id"), nullable=False),
        sa.Column("clarity", sa.Integer(), nullable=False),
        sa.Column("progressed", sa.String(8), nullable=False),
        sa.Column("honesty", sa.String(9), nullable=False),
        sa.Column("best", sa.Text(), nullable=False, server_default=""),
        sa.Column("next_topics", sa.Text(), nullable=False, server_default=""),
        sa.Column("overall", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("session_id", "student_id", name="uq_session_feedback"),
        sa.CheckConstraint("progressed IN ('yes','partial','no')", name="ck_feedback_progressed"),
        sa.CheckConstraint("honesty IN ('ok','violation')", name="ck_feedback_honesty"),
        sa.CheckConstraint("clarity BETWEEN 1 AND 5", name="ck_feedback_clarity"),
        sa.CheckConstraint("overall BETWEEN 1 AND 5", name="ck_feedback_overall"),
    )
    op.create_index("ix_session_feedback_session_id", "session_feedback", ["session_id"])
    op.create_index("ix_session_feedback_student_id", "session_feedback", ["student_id"])
    op.create_index("ix_session_feedback_mentor_id", "session_feedback", ["mentor_id"])
    op.create_index("ix_session_feedback_created_at", "session_feedback", ["created_at"])


def downgrade() -> None:
    if "session_feedback" in _tables():
        op.drop_table("session_feedback")
