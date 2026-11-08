"""Слайс 3: поля отчёта п. 9.3 (insights, focus).

Идемпотентна: на базе после create_all колонки уже есть.

Revision ID: 0004
Revises: 0003
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "mentor_weekly_reports" not in set(inspect(op.get_bind()).get_table_names()):
        return
    columns = _columns("mentor_weekly_reports")
    if "insights" not in columns:
        op.add_column("mentor_weekly_reports", sa.Column("insights", sa.Text(), nullable=False,
                                                         server_default=""))
    if "focus" not in columns:
        op.add_column("mentor_weekly_reports", sa.Column("focus", sa.Text(), nullable=False,
                                                         server_default=""))


def downgrade() -> None:
    if "mentor_weekly_reports" not in set(inspect(op.get_bind()).get_table_names()):
        return
    columns = _columns("mentor_weekly_reports")
    if "focus" in columns:
        op.drop_column("mentor_weekly_reports", "focus")
    if "insights" in columns:
        op.drop_column("mentor_weekly_reports", "insights")
