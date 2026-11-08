"""Исторические записи о людях без учётной записи в боте.

Добавочная ревизия: только новая таблица. Telegram ID не создаётся;
запись хранит имя, username, контур, должность, статус и источник.

Revision ID: 0009
Revises: 0008
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if 'users' not in tables or 'staff_history' in tables:
        return
    op.create_table(
        'staff_history',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('display_name', sa.String(128), nullable=False),
        sa.Column('username', sa.String(64), nullable=True),
        sa.Column('team', sa.String(24), nullable=True),
        sa.Column('title', sa.String(120), nullable=True),
        sa.Column('status', sa.String(16), nullable=True),
        sa.Column('source', sa.String(200), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('created_by', sa.BigInteger(), sa.ForeignKey('users.telegram_id'), nullable=True),
        sa.Column('updated_by', sa.BigInteger(), sa.ForeignKey('users.telegram_id'), nullable=True),
        sa.CheckConstraint("status IS NULL OR status IN ('active','paused','left')",
                           name='ck_staff_history_status'),
        sa.CheckConstraint(
            "team IS NULL OR team IN ('hard','hard_leadership','school_leadership')",
            name='ck_staff_history_team'),
        sa.UniqueConstraint('username', name='uq_staff_history_username'),
    )
    op.create_index('ix_staff_history_username', 'staff_history', ['username'])


def downgrade() -> None:
    tables = _tables()
    if 'staff_history' in tables:
        op.execute('DROP INDEX IF EXISTS ix_staff_history_username')
        op.drop_table('staff_history')
