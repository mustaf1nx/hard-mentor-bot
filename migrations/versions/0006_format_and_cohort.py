"""Слайс 6: формат занятия (п. 8.1.2) и размер потока у триместра.

Без формата нельзя посчитать охват Syllabus Clinic (п. 11.3),
без потока — знаменатель этого охвата.

Revision ID: 0006
Revises: 0005
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def _tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "mentoring_sessions" in _tables() and "format" not in _columns("mentoring_sessions"):
        op.add_column("mentoring_sessions",
                      sa.Column("format", sa.String(24), nullable=False, server_default="one_on_one"))
    if "terms" in _tables() and "cohort" not in _columns("terms"):
        op.add_column("terms", sa.Column("cohort", sa.Integer(), nullable=False, server_default="2500"))


def downgrade() -> None:
    if "mentoring_sessions" in _tables() and "format" in _columns("mentoring_sessions"):
        op.drop_column("mentoring_sessions", "format")
    if "terms" in _tables() and "cohort" in _columns("terms"):
        op.drop_column("terms", "cohort")
