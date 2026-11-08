"""Слайс 5: итоговый отчёт ментора (Прил. 4).

Идемпотентна: на базе после create_all таблица уже есть.

Revision ID: 0005
Revises: 0004
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "mentor_final_reports" in set(inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "mentor_final_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mentor_id", sa.BigInteger(), sa.ForeignKey("users.telegram_id"), nullable=False),
        sa.Column("term_id", sa.Integer(), sa.ForeignKey("terms.id"), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False, server_default="hard"),
        sa.Column("role", sa.String(12), nullable=True),
        sa.Column("slots", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("events", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("collabs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("materials", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checkpoints", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("audience_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("audience_first", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("difficulties", sa.Text(), nullable=False, server_default=""),
        sa.Column("proposals", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("mentor_id", "term_id", name="uq_final_report"),
    )
    op.create_index("ix_mentor_final_reports_mentor_id", "mentor_final_reports", ["mentor_id"])
    op.create_index("ix_mentor_final_reports_term_id", "mentor_final_reports", ["term_id"])


def downgrade() -> None:
    if "mentor_final_reports" in set(inspect(op.get_bind()).get_table_names()):
        op.drop_table("mentor_final_reports")
