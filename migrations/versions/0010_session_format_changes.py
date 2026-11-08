"""История подтверждения формата занятия.

Добавочная ревизия: только новая таблица. Старые занятия не меняются;
запись появляется лишь при явном подтверждении администратором.

Revision ID: 0010
Revises: 0009
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if 'users' not in tables or 'mentoring_sessions' not in tables or 'session_format_changes' in tables:
        return
    op.create_table(
        'session_format_changes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('session_id', sa.Integer(), sa.ForeignKey('mentoring_sessions.id'), nullable=False),
        sa.Column('old_format', sa.String(24), nullable=True),
        sa.Column('old_confirmed', sa.Boolean(), nullable=True),
        sa.Column('new_format', sa.String(24), nullable=False),
        sa.Column('changed_by', sa.BigInteger(), sa.ForeignKey('users.telegram_id'), nullable=False),
        sa.Column('changed_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_session_format_changes_session_id', 'session_format_changes', ['session_id'])
    op.create_index('ix_session_format_changes_changed_by', 'session_format_changes', ['changed_by'])
    op.create_index('ix_session_format_changes_changed_at', 'session_format_changes', ['changed_at'])


def downgrade() -> None:
    if 'session_format_changes' in _tables():
        op.drop_table('session_format_changes')
